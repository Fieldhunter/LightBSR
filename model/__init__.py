import os
from importlib import import_module

import torch
import torch.nn as nn


class Model(nn.Module):
    def __init__(self, args, ckp, loader):
        super(Model, self).__init__()
        print('Making model...')
        self.args = args
        self.scale = args.scale
        self.idx_scale = 0
        self.self_ensemble = args.self_ensemble
        self.chop = args.chop
        self.precision = args.precision
        self.cpu = args.cpu
        self.device = torch.device('cpu' if args.cpu else 'cuda')
        self.n_GPUs = args.n_GPUs
        self.save_models = args.save_models
        self.save = args.save

        module = import_module('model.' + args.model)

        if not args.test_only:
            if args.n_GPUs > 1:
                self.model = module.make_model(args).cuda()
            else:
                self.model = module.make_model(args).to(self.device)
        else:
            self.model = module.make_model(args).to(self.device)

        if args.precision == 'half': self.model.half()

        if not args.test_only and args.initparams:
            model_teacher = torch.load(os.path.join(
                    args.tea_path, 'model/model_700.pt'), map_location='cuda:{}'.format(loader.local_rank))
            new_state_dict = {}
            for key in model_teacher.keys():
                if '.encoder_q' in key:
                    new_state_dict[key.replace('.encoder_q', '')] = model_teacher[key]
                else:
                    new_state_dict[key] = model_teacher[key]
            new_state_dict.pop('E.E.0.weight')
            new_state_dict.pop('E.E.0.bias')
            self.model.load_state_dict(new_state_dict, strict=False)

        if args.n_GPUs > 1:
            if args.resume > 0 and not args.test_only:
                self.model.load_state_dict(
                    torch.load(os.path.join(ckp.dir, 'model', 'model_{}.pt'.format(args.resume)),
                               map_location='cuda:{}'.format(loader.local_rank)),
                    strict=True
                )
        else:
            if args.resume > 0 and not args.test_only:
                self.model.load_state_dict(
                    torch.load(os.path.join(ckp.dir, 'model', 'model_{}.pt'.format(args.resume))),
                    strict=True
                )

        if not args.cpu and args.n_GPUs > 1:
            self.model = torch.nn.parallel.DistributedDataParallel(self.model, [loader.local_rank])

        if args.test_only:
            self.load(
                ckp.dir,
                pre_train=args.pre_train,
                resume=args.resume,
                cpu=args.cpu
            )

    def forward(self, x):
        if self.self_ensemble and not self.training:
            if self.chop:
                forward_function = self.forward_chop
            else:
                forward_function = self.model.forward

            return self.forward_x8(x, forward_function)
        elif self.chop and not self.training:
            return self.forward_chop(x)
        else:
            return self.model(x)

    def get_model(self):
        if self.n_GPUs <= 1 or self.cpu:
            return self.model
        else:
            return self.model.module

    def state_dict(self, **kwargs):
        target = self.get_model()
        return target.state_dict(**kwargs)

    def save(self, apath, epoch, is_best=False):
        target = self.get_model()
        torch.save(
            target.state_dict(),
            os.path.join(apath, 'model', 'model_latest.pt')
        )
        if is_best:
            torch.save(
                target.state_dict(),
                os.path.join(apath, 'model', 'model_best.pt')
            )

        if self.save_models:
            torch.save(
                target.state_dict(),
                os.path.join(apath, 'model', 'model_{}.pt'.format(epoch))
            )

    def load(self, apath, pre_train='.', resume=-1, cpu=False):
        if cpu:
            kwargs = {'map_location': lambda storage, loc: storage}
        else:
            kwargs = {}

        if resume == -1:
            self.get_model().load_state_dict(
                torch.load(os.path.join(apath, 'model', 'model_latest.pt'), **kwargs),
                strict=True
            )

        elif resume == 0:
            if pre_train != '.':
                self.get_model().load_state_dict(
                    torch.load(pre_train, **kwargs),
                    strict=True
                )

        elif resume > 0:
            self.get_model().load_state_dict(
                torch.load(os.path.join(apath, 'model', 'model_{}.pt'.format(resume)), **kwargs),
                strict=True
            )

    def forward_chop(self, x, shave=10, min_size=160000):
        scale = self.scale[self.idx_scale]
        n_GPUs = min(self.n_GPUs, 4)
        b, c, h, w = x.size()
        h_half, w_half = h // 2, w // 2
        h_size, w_size = h_half + shave, w_half + shave
        lr_list = [
            x[:, :, 0:h_size, 0:w_size],
            x[:, :, 0:h_size, (w - w_size):w],
            x[:, :, (h - h_size):h, 0:w_size],
            x[:, :, (h - h_size):h, (w - w_size):w]]

        if w_size * h_size < min_size:
            sr_list = []
            for i in range(0, 4, n_GPUs):
                lr_batch = torch.cat(lr_list[i:(i + n_GPUs)], dim=0)
                sr_batch = self.model(lr_batch)
                sr_list.extend(sr_batch.chunk(n_GPUs, dim=0))
        else:
            sr_list = [
                self.forward_chop(patch, shave=shave, min_size=min_size) \
                for patch in lr_list
            ]

        h, w = scale * h, scale * w
        h_half, w_half = scale * h_half, scale * w_half
        h_size, w_size = scale * h_size, scale * w_size
        shave *= scale

        output = x.new(b, c, h, w)
        output[:, :, 0:h_half, 0:w_half] \
            = sr_list[0][:, :, 0:h_half, 0:w_half]
        output[:, :, 0:h_half, w_half:w] \
            = sr_list[1][:, :, 0:h_half, (w_size - w + w_half):w_size]
        output[:, :, h_half:h, 0:w_half] \
            = sr_list[2][:, :, (h_size - h + h_half):h_size, 0:w_half]
        output[:, :, h_half:h, w_half:w] \
            = sr_list[3][:, :, (h_size - h + h_half):h_size, (w_size - w + w_half):w_size]

        return output

    def forward_x8(self, x, forward_function):
        def _transform(v, op):
            if self.precision != 'single': v = v.float()

            v2np = v.data.cpu().numpy()
            if op == 'v':
                tfnp = v2np[:, :, :, ::-1].copy()
            elif op == 'h':
                tfnp = v2np[:, :, ::-1, :].copy()
            elif op == 't':
                tfnp = v2np.transpose((0, 1, 3, 2)).copy()

            ret = torch.Tensor(tfnp).to(self.device)
            if self.precision == 'half': ret = ret.half()

            return ret

        lr_list = [x]
        for tf in 'v', 'h', 't':
            lr_list.extend([_transform(t, tf) for t in lr_list])

        sr_list = [forward_function(aug) for aug in lr_list]
        for i in range(len(sr_list)):
            if i > 3:
                sr_list[i] = _transform(sr_list[i], 't')
            if i % 4 > 1:
                sr_list[i] = _transform(sr_list[i], 'h')
            if (i % 4) % 2 == 1:
                sr_list[i] = _transform(sr_list[i], 'v')

        output_cat = torch.cat(sr_list, dim=0)
        output = output_cat.mean(dim=0, keepdim=True)

        return output
