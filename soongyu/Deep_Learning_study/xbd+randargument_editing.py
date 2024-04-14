import itertools
import json
import logging
import math
import os
from collections import OrderedDict

import torch
from torch import nn, optim
from torch.nn.parallel.data_parallel import DataParallel

from tqdm import tqdm
from theconf import Config as C, ConfigArgumentParser

from RandAugment.common import get_logger
from RandAugment.data import get_dataloaders
from RandAugment.lr_scheduler import adjust_learning_rate_resnet
from RandAugment.metrics import accuracy, Accumulator
from RandAugment.networks import get_model, num_class
from warmup_scheduler import GradualWarmupScheduler

from RandAugment.common import add_filehandler
from RandAugment.smooth_ce import SmoothCrossEntropyLoss

logger = get_logger('RandAugment')
logger.setLevel(logging.INFO)

def run_epoch(model, loader, loss_fn, optimizer, desc_default='', epoch=0, writer=None, verbose=1, scheduler=None):
    tqdm_disable = bool(os.environ.get('TASK_NAME', ''))    # KakaoBrain Environment
    if verbose:
        loader = tqdm(loader, disable=tqdm_disable)
        loader.set_description('[%s %04d/%04d]' % (desc_default, epoch, C.get()['epoch']))

    metrics = Accumulator()
    cnt = 0
    total_steps = len(loader)
    steps = 0
    for data, label in loader:
        steps += 1
        data, label = data.cuda(), label.cuda()

        if optimizer:
            optimizer.zero_grad()

        preds = model(data)
        loss = loss_fn(preds, label)

        if optimizer:
            loss.backward()
            if C.get()['optimizer'].get('clip', 5) > 0:
                nn.utils.clip_grad_norm_(model.parameters(), C.get()['optimizer'].get('clip', 5))
            optimizer.step()

        top1, top5 = accuracy(preds, label, (1, 5))
        metrics.add_dict({
            'loss': loss.item() * len(data),
            'top1': top1.item() * len(data),
            'top5': top5.item() * len(data),
        })
        cnt += len(data)
        if verbose:
            postfix = metrics / cnt
            if optimizer:
                postfix['lr'] = optimizer.param_groups[0]['lr']
            loader.set_postfix(postfix)

        if scheduler is not None:
            scheduler.step(epoch - 1 + float(steps) / total_steps)

        del preds, loss, top1, top5, data, label

    if tqdm_disable:
        if optimizer:
            logger.info('[%s %03d/%03d] %s lr=%.6f', desc_default, epoch, C.get()['epoch'], metrics / cnt, optimizer.param_groups[0]['lr'])
        else:
            logger.info('[%s %03d/%03d] %s', desc_default, epoch, C.get()['epoch'], metrics / cnt)

    metrics /= cnt
    if optimizer:
        metrics.metrics['lr'] = optimizer.param_groups[0]['lr']
    if verbose:
        for key, value in metrics.items():
            writer.add_scalar(key, value, epoch)
    return metrics


def train_and_eval(tag, dataroot, test_ratio=0.0, cv_fold=0, reporter=None, metric='last', save_path=None, only_eval=False):
    if not reporter:
        reporter = lambda **kwargs: 0

    max_epoch = C.get()['epoch']
    trainsampler, trainloader, validloader, testloader_ = get_dataloaders(C.get()['dataset'], C.get()['batch'], dataroot, test_ratio, split_idx=cv_fold)

    # create a model & an optimizer
    model = get_model(C.get()['model'], num_class(C.get()['dataset']))

    lb_smooth = C.get()['optimizer'].get('label_smoothing', 0.0)
    if lb_smooth > 0.0:
        criterion = SmoothCrossEntropyLoss(lb_smooth)
    else:
        criterion = nn.CrossEntropyLoss()
    if C.get()['optimizer']['type'] == 'sgd':
        optimizer = optim.SGD(
            model.parameters(),
            lr=C.get()['lr'],
            momentum=C.get()['optimizer'].get('momentum', 0.9),
            weight_decay=C.get()['optimizer']['decay'],
            nesterov=C.get()['optimizer']['nesterov']
        )
    else:
        raise ValueError('invalid optimizer type=%s' % C.get()['optimizer']['type'])

    if C.get()['optimizer'].get('lars', False):
        from torchlars import LARS
        optimizer = LARS(optimizer)
        logger.info('*** LARS Enabled.')

    lr_scheduler_type = C.get()['lr_schedule'].get('type', 'cosine')
    if lr_scheduler_type == 'cosine':
        scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=C.get()['epoch'], eta_min=0.)
    elif lr_scheduler_type == 'resnet':
        scheduler = adjust_learning_rate_resnet(optimizer)
    else:
        raise ValueError('invalid lr_schduler=%s' % lr_scheduler_type)

    if C.get()['lr_schedule'].get('warmup', None):
        scheduler = GradualWarmupScheduler(
            optimizer,
            multiplier=C.get()['lr_schedule']['warmup']['multiplier'],
            total_epoch=C.get()['lr_schedule']['warmup']['epoch'],
            after_scheduler=scheduler
        )

    if not tag:
        from RandAugment.metrics import SummaryWriterDummy as SummaryWriter
        logger.warning('tag not provided, no tensorboard log.')
    else:
        from tensorboardX import SummaryWriter
    writers = [SummaryWriter(log_dir='./logs/%s/%s' % (tag, x)) for x in ['train', 'valid', 'test']]

    result = OrderedDict()
    epoch_start = 1
    if save_path and os.path.exists(save_path):
        logger.info('%s file found. loading...' % save_path)
        data = torch.load(save_path)
        if 'model' in data or 'state_dict' in data:
            key = 'model' if 'model' in data else 'state_dict'
            model.load_state_dict({k if 'module.' in k else 'module.'+k: v for k, v in data[key].items()})
            if metric in data:
                result['best_valid'] = data[metric]
        else:
            model.load_state_dict({k if 'module.' in k else 'module.'+k: v for k, v in data.items()})
        if 'optimizer' in data:
            optimizer.load_state_dict(data['optimizer'])
        if 'scheduler' in data:
            scheduler.load_state_dict(data['scheduler'])
        if 'epoch' in data:
            epoch_start = data['epoch'] + 1

    # just validate
    if only_eval:
        logger.info('evaluation only mode. just exit after validation')
        model.cuda()
        model.eval()
        with torch.no_grad():
            valid_metrics = run_epoch(model, validloader, criterion, None, desc_default='valid', epoch=epoch_start, writer=None, verbose=1)
            test_metrics = run_epoch(model, testloader_, criterion, None, desc_default='test ', epoch=epoch_start, writer=None, verbose=1)
        logger.info('[valid]')
        for key, value in valid_metrics.items():
            logger.info('%s = %.4f', key, value)
        logger.info('[test ]')
        for key, value in test_metrics.items():
            logger.info('%s = %.4f', key, value)
        logger.info('evaluation done.')
        return valid_metrics

    # train & validate
    logger.info('start training, epoch=%d' % max_epoch)
    for epoch in range(epoch_start, max_epoch + 1):
        model.cuda()
        model.train()
        train_metrics = run_epoch(model, trainloader, criterion, optimizer, desc_default='train', epoch=epoch, writer=writers[0])
        model.eval()
        with torch.no_grad():
            valid_metrics = run_epoch(model, validloader, criterion, None, desc_default='valid', epoch=epoch, writer=writers[1])

        logger.info('[train]')
        for key, value in train_metrics.items():
            logger.info('%s = %.4f', key, value)
        logger.info('[valid]')
        for key, value in valid_metrics.items():
            logger.info('%s = %.4f', key, value)

        # save if best accuracy
        if 'best_valid' not in result or result['best_valid'] < valid_metrics[metric]:
            logger.info('epoch=%d update best acc=%.4f -> %.4f', epoch, result.get('best_valid', -1), valid_metrics[metric])
            if save_path:
                torch.save({
                    'epoch': epoch,
                    metric: valid_metrics[metric],
                    'model': model.state_dict(),
                    'optimizer': optimizer.state_dict(),
                    'scheduler': scheduler.state_dict()
                }, save_path)
            result['best_valid'] = valid_metrics[metric]

        if C.get()['epoch'] - epoch <= 5:
            test_metrics = run_epoch(model, testloader_, criterion, None, desc_default='test ', epoch=epoch, writer=writers[2])
            logger.info('[test ]')
            for key, value in test_metrics.items():
                logger.info('%s = %.4f', key, value)
        else:
            test_metrics = None

        reporter(epoch=epoch, train=train_metrics, valid=valid_metrics, test=test_metrics)
        scheduler.step(epoch)

    return result

if __name__ == '__main__':
    parser = ConfigArgumentParser(conflict_handler='resolve')
    parser.add_argument('--conf', '-c', type=str, help='config file path')
    args = parser.parse_args()

    if args.conf:
        C.get().load(args.conf)

    train_and_eval(os.path.basename(args.conf).split('.')[0], './data', only_eval=False)
