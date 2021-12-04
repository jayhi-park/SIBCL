"""
The top-level model of training-time PixLoc.
Encapsulates the feature extraction, pose optimization, loss and metrics.
"""
import torch
from torch.nn import functional as nnF
import logging
from copy import deepcopy
import omegaconf
import numpy as np

from pixloc.pixlib.models.base_model import BaseModel
from pixloc.pixlib.models import get_model
from pixloc.pixlib.models.utils import masked_mean
from pixloc.pixlib.geometry.losses import scaled_barron

logger = logging.getLogger(__name__)

# add by shan
share_weight = True

class TwoViewRefiner(BaseModel):
    default_conf = {
        'extractor': {
            'name': 's2dnet',
        },
        'optimizer': {
            'name': 'basic_optimizer',
        },
        'duplicate_optimizer_per_scale': False,
        'success_thresh': 2,
        'clamp_error': 50,
        'normalize_features': True,
        'normalize_dt': True,

        # deprecated entries
        'init_target_offset': None,
    }
    required_data_keys = {
        'ref': ['image', 'camera', 'T_w2cam'],
        'query': ['image', 'camera', 'T_w2cam'],
    }
    strict_conf = False  # need to pass new confs to children models

    def _init(self, conf):
        self.extractor = get_model(conf.extractor.name)(conf.extractor)
        assert hasattr(self.extractor, 'scales')
        if not share_weight: 
            self.extractor_sat = deepcopy(self.extractor) # add by shan

        Opt = get_model(conf.optimizer.name)
        if conf.duplicate_optimizer_per_scale:
            oconfs = [deepcopy(conf.optimizer) for _ in self.extractor.scales]
            feature_dim = self.extractor.conf.output_dim
            if not isinstance(feature_dim, int):
                for d, oconf in zip(feature_dim, oconfs):
                    with omegaconf.read_write(oconf):
                        with omegaconf.open_dict(oconf):
                            oconf.feature_dim = d
            self.optimizer = torch.nn.ModuleList([Opt(c) for c in oconfs])
        else:
            self.optimizer = Opt(conf.optimizer)

        if conf.init_target_offset is not None:
            raise ValueError('This entry has been deprecated. Please instead '
                             'use the `init_pose` config of the dataloader.')

    def _forward(self, data):
        def process_siamese(data_i):
            pred_i = self.extractor(data_i)
            pred_i['camera_pyr'] = [data_i['camera'].scale(1/s)
                                    for s in self.extractor.scales]
            return pred_i

        # change by shan
        if share_weight: 
            pred = {i: process_siamese(data[i]) for i in ['ref', 'query']}
        else:

            # add by shan for satellite image extractor
            def process_sat(data_i):
                pred_i = self.extractor_sat(data_i)
                pred_i['camera_pyr'] = [data_i['camera'].scale(1/s)
                                    for s in self.extractor_sat.scales]
                return pred_i
            pred = {i: process_siamese(data[i]) for i in ['query']}
            pred.update({i: process_sat(data[i]) for i in ['ref']})

        p3D_ref = data['ref']['points3D']
        T_init = data['T_r2q_init']

        pred['T_r2q_init'] = []
        pred['T_r2q_opt'] = []
        pred['valid_masks'] = []
        for i in reversed(range(len(self.extractor.scales))):
            F_ref = pred['ref']['feature_maps'][i]
            F_q = pred['query']['feature_maps'][i]
            cam_ref = pred['ref']['camera_pyr'][i]
            cam_q = pred['query']['camera_pyr'][i]
            if self.conf.duplicate_optimizer_per_scale:
                opt = self.optimizer[i]
            else:
                opt = self.optimizer

            p2D_ref, visible = cam_ref.world2image(p3D_ref)
            F_ref, mask, _ = opt.interpolator(F_ref, p2D_ref)
            mask &= visible

            W_ref_q = None
            if self.extractor.conf.get('compute_uncertainty', False):
                W_ref = pred['ref']['confidences'][i]
                W_q = pred['query']['confidences'][i]
                W_ref, _, _ = opt.interpolator(W_ref, p2D_ref)
                W_ref_q = (W_ref, W_q)

            if self.conf.normalize_features:
                F_ref = nnF.normalize(F_ref, dim=2)  # B x N x C
                F_q = nnF.normalize(F_q, dim=1)  # B x C x W x H

            T_opt, failed = opt(dict(
                p3D=p3D_ref, F_ref=F_ref, F_q=F_q, T_init=T_init, cam_q=cam_q,
                mask=mask, W_ref_q=W_ref_q))

            pred['T_r2q_init'].append(T_init)
            pred['T_r2q_opt'].append(T_opt)
            T_init = T_opt.detach()
        return pred

    # add by shan for satellite image extractor
    def add_sat_extractor(self):
        self.extractor_sat = deepcopy(self.extractor)
        for param in self.extractor_sat.parameters():
            param.requires_grad = True

    def loss(self, pred, data):
        cam_q = data['query']['camera']

        def project(T_r2q):
            return cam_q.world2image(T_r2q * data['ref']['points3D'])

        p2D_q_gt, mask = project(data['T_r2q_gt'])
        p2D_q_i, mask_i = project(data['T_r2q_init'])
        mask = (mask & mask_i).float()

        too_few = torch.sum(mask, -1) < 10
        if torch.any(too_few):
            logger.warning('Few points in batch '+str(data['scene']))
            # logger.warning(
            #     'Few points in batch '+str([
            #         (data['scene'][i], data['ref']['index'][i].item(),
            #          data['query']['index'][i].item())
            #         for i in torch.where(too_few)[0]]))

        def reprojection_error(T_r2q):
            p2D_q, _ = project(T_r2q)
            err = torch.sum((p2D_q_gt - p2D_q)**2, dim=-1)
            err = scaled_barron(1., 2.)(err)[0]/4
            err = masked_mean(err, mask, -1)
            return err

        num_scales = len(self.extractor.scales)
        success = None
        losses = {'total': 0.}
        for i, T_opt in enumerate(pred['T_r2q_opt']):
            err = reprojection_error(T_opt).clamp(max=self.conf.clamp_error)
            loss = err / num_scales
            if i > 0:
                loss = loss * success.float()
            thresh = self.conf.success_thresh * self.extractor.scales[-1-i]
            success = err < thresh
            losses[f'reprojection_error/{i}'] = err
            losses['total'] += loss
        losses['reprojection_error'] = err
        losses['total'] *= (~too_few).float()

        err_init = reprojection_error(pred['T_r2q_init'][0])
        losses['reprojection_error/init'] = err_init

        return losses

    def metrics(self, pred, data):
        T_q2r_gt = data['ref']['T_w2cam'] @ data['query']['T_w2cam'].inv()

        @torch.no_grad()
        def scaled_pose_error(T_r2q):
            err_R, err_t = (T_r2q @ T_q2r_gt).magnitude()
            err_x = (T_r2q @ T_q2r_gt).magnitude_lateral()
            if self.conf.normalize_dt:
                err_t /= torch.norm(T_q2r_gt.t, dim=-1)
            # change for validate lateral error only, change by shan
            # return err_R, err_t
                err_x /= T_q2r_gt.magnitude_lateral()
            return err_R, err_x

        metrics = {}
        for i, T_opt in enumerate(pred['T_r2q_opt']):
            err = scaled_pose_error(T_opt)
            metrics[f'R_error/{i}'], metrics[f't_error/{i}'] = err
        metrics['R_error'], metrics['t_error'] = err

        err_init = scaled_pose_error(pred['T_r2q_init'][0])
        metrics['R_error/init'], metrics['t_error/init'] = err_init

        return metrics
