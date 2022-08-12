import datetime
import torch.nn.functional as F
from tqdm import tqdm
import network
import utils
import os
import random
import argparse
import numpy as np
from utils.loss_history import LossHistory
from thop import profile
from pytorch_model_summary import summary
from ptflops import get_model_complexity_info
from torchstat import stat

from torch.utils import data
from datasets import VOCSegmentation, Cityscapes
from utils import ext_transforms as et
# from utils import OneHot
from metrics import StreamSegMetrics
from LOSS.ccnet_loss.criterion import CriterionDSN, CriterionOhemDSN


# from CPF_dataset import OCT
from network.CPF_model.BaseNet import CPFNet
from network.CPF_model.unet import UNet
from network.dfn_models import DFN
from network.CCNet import ccnet
from network.GC_BLSA import GC_BLSA
from network.PSPNet import pspnet
from network.Medical_Transformer import axialnet
from network.DAnet.sseg.danet import DANet
from network.axial_deeplab import axial50l
from network.TransUnet.vit_seg_modeling import VisionTransformer as Vit_seg
# from network.DAnet.sseg import danet
from network.TransUnet.vit_seg_modeling import CONFIGS as CONFIGS_ViT_seg
from network.SETR.transformer_seg import SETRModel
from network.non_local.res import Non_local
# from datasets.voc import pspnet_dataset_collate
import torch
import torch.nn as nn
#from torchvision.transforms import InterpolationMode

from utils.visualizer import Visualizer

from PIL import Image
import matplotlib
import matplotlib.pyplot as plt


def get_argparser():
    parser = argparse.ArgumentParser()

    # Datset Options
    parser.add_argument("--data_root", type=str, default='./datasets/data',
                        help="数据集路径")
    parser.add_argument("--dataset", type=str, default='voc',
                        choices=['voc', 'cityscapes'],
                        help='数据集名称')
    parser.add_argument("--num_classes", type=int, default=5,
                        help="类别数 (default: None)")

    # Deeplab Options
    parser.add_argument("--model", type=str, default='',
                        choices=['deeplabv3_resnet50',  'deeplabv3plus_resnet50',
                                 'deeplabv3_resnet101', 'deeplabv3plus_resnet101',
                                 'deeplabv3_mobilenet', 'deeplabv3plus_mobilenet'], help='使用模型')

    # other model
    parser.add_argument("--other_model", type=str, default='CCNet',
                        choices=['CPF','DFN','DANet', 'CCNet','PSPnet','MedT',
                                 'axial_deeplab', 'TransUnet','SETR','non_local', 'GC_BLSA'], help='其他模型')

    parser.add_argument("--separable_conv", action='store_true', default=False,
                        help="在aspp和解码器中使用可分离卷积")
    parser.add_argument("--output_stride", type=int, default=16, choices=[8, 16], help='输出步长')

    # Train Options
    parser.add_argument("--test_only", action='store_true', default=False)
    parser.add_argument("--save_val_results", action='store_true', default=False,
                        help="保存分割结果 \"./results\"")
    parser.add_argument("--total_itrs", type=int, default=66000,
                        help="epoch数量(default: 30k)")
    parser.add_argument("--lr", type=float, default=0.01,
                        help="学习率 (default: 0.01)")
    parser.add_argument("--lr_policy", type=str, default='poly', choices=['poly', 'step'],
                        help="学习率衰减策略")
    parser.add_argument("--step_size", type=int, default=10000)
    parser.add_argument("--crop_val", action='store_true', default=True,
                        help='裁剪验证集 (default: False)')
    parser.add_argument("--batch_size", type=int, default=8,
                        help='batch size (default: 16)')
    parser.add_argument("--val_batch_size", type=int, default=8,
                        help='验证集批次大小 (default: 4)')

    parser.add_argument("--crop_size", default=(512,512))
    parser.add_argument("--crop_size_long", type=int, default=256)
    parser.add_argument("--crop_size_width", type=int, default=192)

    parser.add_argument("--ckpt", default=None, type=str,
                        help="restore from checkpoint")
    parser.add_argument("--continue_training", action='store_true', default=False)

    parser.add_argument("--loss_type", type=str, default='cross_entropy',
                        choices=['cross_entropy', 'focal_loss', 'softmaxloss', 'CE_Loss', 'Dice_Loss', ''], help="损失函数 (default: False)")
    parser.add_argument("--gpu_id", type=str, default='0,1',
                        help="GPU ID（GPU数量）")
    parser.add_argument("--weight_decay", type=float, default=1e-4,
                        help='weight decay9（权重衰减） (default: 1e-4)')
    parser.add_argument("--random_seed", type=int, default=1,
                        help="random seed（随机数种子） (default: 1)")
    parser.add_argument("--print_interval", type=int, default=10,
                        help="print interval of loss (default: 10)")
    parser.add_argument("--val_interval", type=int, default=100,
                        help="epoch interval for eval (default: 100)")
    parser.add_argument("--download", action='store_true', default=False,
                        help="download datasets（下载数据集）")

    # PASCAL VOC Options
    parser.add_argument("--year", type=str, default='2012',
                        choices=['2012_aug', '2012', '2011', '2009', '2008', '2007'], help='year of VOC')

    # Visdom options
    parser.add_argument("--enable_vis", action='store_true', default=False,
                        help="use visdom for visualization")
    parser.add_argument("--vis_port", type=str, default='13570',
                        help='port for visdom')
    parser.add_argument("--vis_env", type=str, default='main',
                        help='env for visdom')
    parser.add_argument("--vis_num_samples", type=int, default=8,
                        help='number of samples for visualization (default: 8)')
    return parser


def get_dataset(opts):
    """ Dataset And Augmentation
    """
    if opts.dataset == 'voc':
        train_transform = et.ExtCompose([
            et.ExtResize(size=opts.crop_size[0]),
            et.ExtRandomScale((0.5, 2.0)),
            et.ExtRandomCrop(size=(opts.crop_size), pad_if_needed=True),
            et.ExtRandomHorizontalFlip(),
            #et.AddGaussionNoise(mean=0, variance=1, amplitude=20),
            et.ExtToTensor(),
            et.ExtNormalize(mean=[0.485, 0.456, 0.406],
                            std=[0.229, 0.224, 0.225]),
        ])
        if opts.crop_val:
            val_transform = et.ExtCompose([
                et.ExtResize(opts.crop_size[0]),
                et.ExtCenterCrop(opts.crop_size[0]),
                #et.AddGaussionNoise(mean=0, variance=1, amplitude=20),
                et.ExtToTensor(),
                et.ExtNormalize(mean=[0.485, 0.456, 0.406],
                                std=[0.229, 0.224, 0.225]),
            ])
        else:
            val_transform = et.ExtCompose([
                et.ExtToTensor(),
                et.ExtNormalize(mean=[0.485, 0.456, 0.406],
                                std=[0.229, 0.224, 0.225]),
            ])
        train_dst = VOCSegmentation(root=opts.data_root, year=opts.year,
                                    image_set='train', download=opts.download, transform=train_transform)
        val_dst = VOCSegmentation(root=opts.data_root, year=opts.year,
                                  image_set='val', download=False, transform=val_transform)

    if opts.dataset == 'cityscapes':
        train_transform = et.ExtCompose([
            #et.ExtResize( 512 ),
            et.ExtRandomCrop(size=(opts.crop_size[0], opts.crop_size[1])),
            et.ExtColorJitter( brightness=0.5, contrast=0.5, saturation=0.5 ),
            et.ExtRandomHorizontalFlip(),
            et.ExtToTensor(),
            et.ExtNormalize(mean=[0.485, 0.456, 0.406],
                            std=[0.229, 0.224, 0.225]),

        ])

        val_transform = et.ExtCompose([
            #et.ExtResize( 512 ),
            et.ExtToTensor(),
            et.ExtNormalize(mean=[0.485, 0.456, 0.406],
                            std=[0.229, 0.224, 0.225]),
        ])

        train_dst = Cityscapes(root=opts.data_root,
                               split='train', transform=train_transform)
        val_dst = Cityscapes(root=opts.data_root,
                             split='val', transform=val_transform)
    return train_dst, val_dst


def validate(opts, model, loader, device, metrics, loss_history, criterion,ret_samples_ids=None):
    """Do validation and return specified samples"""
    metrics.reset()
    ret_samples = []
    val_iter_loss = 0
    if opts.save_val_results:
        if not os.path.exists('results'):
            os.mkdir('results')
        denorm = utils.Denormalize(mean=[0.485, 0.456, 0.406], 
                                   std=[0.229, 0.224, 0.225])
        img_id = 0

    with torch.no_grad():
        for i, (images, labels) in tqdm(enumerate(loader)):

            images = images.to(device, dtype=torch.float32)
            labels = labels.to(device, dtype=torch.long)
            # _, _, _, _, _, _, outputs = model(images)
            outputs = model(images)
            val_loss1 = criterion[0](outputs[0], labels)
            val_loss2 = criterion[1](outputs[1], labels)
            val_loss = 0.2*val_loss1 + 0.8*val_loss2
            np_loss = val_loss.detach().cpu().numpy()
            val_iter_loss += np_loss

            # outputs = F.interpolate(outputs[0], (512,512), mode='bilinear', align_corners=True)

            preds = outputs[0].detach().max(dim=1)[1].cpu().numpy()
            targets = labels.cpu().numpy()

            metrics.update(targets, preds)
            if ret_samples_ids is not None and i in ret_samples_ids:  # get vis samples
                ret_samples.append(
                    (images[0].detach().cpu().numpy(), targets[0], preds[0]))

            if opts.save_val_results:
                for i in range(len(images)):
                    image = images[i].detach().cpu().numpy()
                    target = targets[i]
                    pred = preds[i]

                    image = (denorm(image) * 255).transpose(1, 2, 0).astype(np.uint8)
                    target = loader.dataset.decode_target(target).astype(np.uint8)
                    pred = loader.dataset.decode_target(pred).astype(np.uint8)

                    Image.fromarray(image).save('results/%d_image.png' % img_id)
                    Image.fromarray(target).save('results/%d_target.png' % img_id)
                    Image.fromarray(pred).save('results/%d_pred.png' % img_id)

                    fig = plt.figure()
                    plt.imshow(image)
                    plt.axis('off')
                    plt.imshow(pred, alpha=0.7)
                    ax = plt.gca()
                    ax.xaxis.set_major_locator(matplotlib.ticker.NullLocator())
                    ax.yaxis.set_major_locator(matplotlib.ticker.NullLocator())
                    plt.savefig('results/%d_overlay.png' % img_id, bbox_inches='tight', pad_inches=0)
                    plt.close()
                    img_id += 1
            j = i
        score = metrics.get_results()
        loss_history.append_val_loss(val_iter_loss / j)
        loss_history.append_miou(score["Mean IoU"])
    return score, ret_samples


def main():
    start = datetime.datetime.now()
    print('begin time: %s'%start)
    opts = get_argparser().parse_args()
    if opts.dataset.lower() == 'voc':
        opts.num_classes = opts.num_classes
    elif opts.dataset.lower() == 'cityscapes':
        opts.num_classes = 19

    # Setup visualization
    vis = Visualizer(port=opts.vis_port,
                     env=opts.vis_env) if opts.enable_vis else None
    if vis is not None:  # display options
        vis.vis_table("Options", vars(opts))

    os.environ['CUDA_VISIBLE_DEVICES'] = opts.gpu_id
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    print("Device: %s" % device)

    # Setup random seed
    torch.manual_seed(opts.random_seed)
    if opts.dataset=='voc' and not opts.crop_val:
        opts.val_batch_size = 1

    train_dst, val_dst = get_dataset(opts)
    train_loader = data.DataLoader(
        train_dst, batch_size=opts.batch_size, drop_last=True, shuffle=True, num_workers=0)
    val_loader = data.DataLoader(
        val_dst, batch_size=opts.val_batch_size, drop_last=True, shuffle=True, num_workers=0)
    # print("train_data:", train_loader.)
    L_train = len(train_dst)
    np.random.seed(opts.random_seed)
    random.seed(opts.random_seed)

    # Setup dataloader
    L_val = len(val_dst)
    print("Dataset: %s, Train set: %d, Val set: %d" %
          (opts.dataset, L_train, L_val))

    # Set up model
    # model_map = {
    #     'deeplabv3_resnet50': network.deeplabv3_resnet50,
    #     'deeplabv3plus_resnet50': network.deeplabv3plus_resnet50,
    #     'deeplabv3_resnet101': network.deeplabv3_resnet101,
    #     'deeplabv3plus_resnet101': network.deeplabv3plus_resnet101,
    #     'deeplabv3_mobilenet': network.deeplabv3_mobilenet,
    #     'deeplabv3plus_mobilenet': network.deeplabv3plus_mobilenet
    # }
    #
    # model = model_map[opts.model](num_classes=opts.num_classes, output_stride=opts.output_stride)
    # if opts.separable_conv and 'plus' in opts.model:
    #     network.convert_to_separable_conv(model.classifier)
    # utils.set_bn_momentum(model.backbone, momentum=0.01)

    if opts.other_model == 'CPF':
        model = CPFNet(out_planes=opts.num_classes)
    elif opts.other_model == 'UNet':
        model = UNet(in_channels=3, n_classes=opts.num_classes)
    elif opts.other_model == 'DFN':
        model = DFN.DFN(num_class=opts.num_classes)
    elif opts.other_model == 'CCNet':
        model = ccnet.ccnet(num_classes=opts.num_classes, recurrence=2)
    elif opts.other_model == 'PSPnet':
        model = pspnet.PSPNet(num_classes=opts.num_classes, backbone='resnet101', downsample_factor=16,
                              pretrained=False, aux_branch=False)
    elif opts.other_model == 'MedT':
        model = axialnet.MedT(img_size = opts.crop_size[0], imgchan = 3, num_classes = opts.num_classes)
        # ResNet(Bottleneck, [3, 4, 23, 3], num_classes, criterion, recurrence)
    elif opts.other_model == 'DANet':
        model = DANet(nclass=opts.num_classes, backbone='resnet101')
    elif opts.other_model == 'axial_deeplab':
        model = axial50l(pretrained=True,num_classes = opts.num_classes)
    elif opts.other_model == 'TransUnet':
        config_vit = CONFIGS_ViT_seg['R50-ViT-B_16']
        config_vit.n_classes = opts.num_classes
        model = Vit_seg(config_vit, img_size=512, num_classes=config_vit.n_classes)
    elif opts.other_model == 'SETR':
        model = SETRModel(patch_size=(32, 32), in_channels=3, out_channels=opts.num_classes, hidden_size=1024,
                          num_hidden_layers=24, num_attention_heads=16, decode_features=[512, 256, 128, 64])
    elif opts.other_model == 'non_local':
        model = Non_local(num_classes=opts.num_classes)
    elif opts.other_model == 'GC_BLSA':
        model = GC_BLSA.GC_BLSA(num_classes=opts.num_classes)
    # Set up metrics
    metrics = StreamSegMetrics(opts.num_classes)

    # Set up optimizer
    # optimizer = torch.optim.SGD(params=[
    #     {'params': model.backbone.parameters(), 'lr': 0.1*opts.lr},
    #     {'params': model.classifier.parameters(), 'lr': opts.lr},
    # ], lr=opts.lr, momentum=0.9, weight_decay=opts.weight_decay)
    optimizer = torch.optim.SGD(params=model.parameters(), lr=opts.lr, momentum=0.9, weight_decay=opts.weight_decay)
    #torch.optim.lr_scheduler.StepLR(optimizer, step_size=opts.lr_decay_step, gamma=opts.lr_decay_factor)

    if opts.lr_policy=='poly':
        scheduler = utils.PolyLR(optimizer, opts.total_itrs, power=0.9)
    elif opts.lr_policy=='step':
        scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=opts.step_size, gamma=0.1)

    # Set up criterion
    #criterion = utils.get_loss(opts.loss_type)
    if opts.loss_type == 'focal_loss':
        criterion = [utils.FocalLoss(ignore_index=255, size_average=True)]
    elif opts.loss_type == 'cross_entropy':
        criterion1 = nn.CrossEntropyLoss(ignore_index=255, reduction='mean')
        criterion2 = utils.FocalLoss(ignore_index=255, size_average=True)
        criterion = [criterion1, criterion2]
    elif opts.loss_type == 'softmaxloss':
        criterion = utils.FocalLoss(ignore_index=255, size_average=True)
        criterion = utils.loss.SmoothNet_Loss()
    elif opts.loss_type == 'CE_Loss':
        # criterion = utils.loss.CE_Loss(opts.num_classes)
        # criterion = utils.loss.CE_Loss(opts.num_classes)
        criterion =nn.NLLLoss(opts.num_classes)
        # criterion_ = nn.NLLLoss()
    elif opts.loss_type == 'Dice_Loss':
        criterion = utils.loss.Dice_Loss(beta=1, smooth=1e-5)
    elif opts.loss_type == 'others':
        criterion = [utils.FocalLoss(ignore_index=255, size_average=True),torch.nn.CrossEntropyLoss()]
    elif opts.loss_type == 'ccnet_loss':
        criterion = CriterionOhemDSN(thresh=0.6, min_kept=200000)
        # criterion1 = utils.FocalLoss(ignore_index=255, size_average=True)
        # criterion2 = utils.loss.SmoothNet_Loss()
    def save_ckpt(path):
        """ save current model
        """
        torch.save({
            "cur_itrs": cur_itrs,
            "model_state": model.module.state_dict(),
            "optimizer_state": optimizer.state_dict(),
            "scheduler_state": scheduler.state_dict(),
            "best_score": best_score,
        }, path)
        print("Model saved as %s" % path)
    
    # utils.mkdir('checkpoints/%s'%opts.other_model)
    pth_file_name = "checkpoints/VOC2012/Flops(backbone)"
    utils.mkdir(pth_file_name)
    # Restore
    best_score = 0.0
    cur_itrs = 0
    cur_epochs = 0

    input = torch.randn(1, 3, 512, 512)
    with open(pth_file_name + "/模型参数.txt", 'w') as f:
        print(summary(model, input, show_input=False, show_hierarchical=False), file=f)
    flops, params = get_model_complexity_info(model, (3, 512, 512), as_strings=True,
                                              print_per_layer_stat=True, ost=pth_file_name + '/模型参数1.txt')
    profile_flops, profile_params = profile(model, inputs=(input,))
    with open(pth_file_name + "/模型参数1.txt", 'a') as f:
        print("===================================================================", file=f)
        print("Total flops: ",flops, file=f)
        print("Totle params: ", params, file=f)
        print("profile_flops ", profile_flops, file=f)
        print("profile_params: ", profile_params, file=f)
        print("-------------------------------------------------------------------", file=f)
    print('Flops: ',flops, 'Params: ',params)
    stat(model, (3, 512, 512), save_path=pth_file_name, save_name='模型参数2')
    total_params = sum(p.numel() for p in model.parameters())
    total_trainable_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    with open(pth_file_name + "/参数统计.txt", 'a') as f:
        print('total_params: ',total_params, file=f)
        print('total_trainable_params: ',total_trainable_params, file=f)

    loss_history = LossHistory(pth_file_name)
    if opts.ckpt is not None and os.path.isfile(opts.ckpt):
        # https://github.com/VainF/DeepLabV3Plus-Pytorch/issues/8#issuecomment-605601402, @PytaichukBohdan
        checkpoint = torch.load(opts.ckpt, map_location=torch.device('cpu'))
        model.load_state_dict(checkpoint["model_state"])
        model = nn.DataParallel(model)
        model.to(device)
        if opts.continue_training:
            optimizer.load_state_dict(checkpoint["optimizer_state"])
            scheduler.load_state_dict(checkpoint["scheduler_state"])
            cur_itrs = checkpoint["cur_itrs"]
            best_score = checkpoint['best_score']
            print("Training state restored from %s" % opts.ckpt)
        print("Model restored from %s" % opts.ckpt)
        del checkpoint  # free memory
    else:
        print("[!] Retrain")
        model = nn.DataParallel(model)
        model.to(device)

    #==========   Train Loop   ==========#
    vis_sample_id = np.random.randint(0, len(val_loader), opts.vis_num_samples,
                                      np.int32) if opts.enable_vis else None  # sample idxs for visualization
    denorm = utils.Denormalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])  # denormalization for ori images

    if opts.test_only:
        model.eval()
        val_score, ret_samples = validate(
            opts=opts, model=model, loader=val_loader, device=device, metrics=metrics, ret_samples_ids=vis_sample_id)
        print(metrics.to_str(val_score))
        return

    interval_loss = 0
    while True: #cur_itrs < opts.total_itrs:
        # =====  Train  =====
        model.train()
        cur_epochs += 1
        # print("train_loader:", train_loader.size())
        for (images, labels) in train_loader:
            cur_itrs += 1

            images = images.to(device, dtype=torch.float32)
            targets = labels.to(device, dtype=torch.long)
            # print(images.size())

            optimizer.zero_grad()
            outputs= model(images)
            # print(outputs.size())
            # print(targets.size())
            loss1 = criterion[0](outputs[0], targets)
            loss2 = criterion[1](outputs[1], targets)
            # loss_history = LossHistory(pth_file_name)
            # loss1 = criterion(outputs, targets)
            # loss2 = criterion(aux_outputs, targets)
            # Loss1 = nn.NLLLoss(ignore_index=opts.num_classes)(F.log_softmax(outputs, dim=1), targets)
            # loss2 = nn.NLLLoss(ignore_index=opts.num_classes)(F.log_softmax(aux_outputs, dim=1), targets)
            loss = 0.2*loss1 + 0.8*loss2
            loss.backward()
            optimizer.step()

            np_loss = loss.detach().cpu().numpy()
            interval_loss += np_loss

            if vis is not None:
                vis.vis_scalar('Loss', cur_itrs, np_loss)

            if (cur_itrs) % 10 == 0:
                interval_loss = interval_loss/10
                loss_history.append_train_loss(interval_loss)
                print("Epoch %d, Itrs %d/%d, Loss=%f" %
                      (cur_epochs, cur_itrs, opts.total_itrs, interval_loss))
                interval_loss = 0.0

            if (cur_itrs) % opts.val_interval == 0:
                save_ckpt(pth_file_name + '/latest_%s_%s_os%d.pth' %
                          (opts.model, opts.dataset, opts.output_stride))
                print("validation...")
                model.eval()
                val_score, ret_samples = validate(
                    opts=opts, model=model, loader=val_loader, device=device, metrics=metrics,
                    loss_history=loss_history ,ret_samples_ids=vis_sample_id, criterion=criterion)
                print()
                print(metrics.to_str(val_score))
                if val_score['Mean IoU'] > best_score:
                    Previous_best_IoU = val_score['Mean IoU']
                    Best_epoch = cur_epochs
                    Best_itrs = cur_itrs
                    Now_Best_val_score = metrics.to_str(val_score)
                    with open(pth_file_name + '/best_result.txt', 'w') as f:
                        print('Now Best Result: ', file=f)
                        print('================== 数据集 =================', file=f)
                        print("Dataset: %s, Train set: %d, Val set: %d" %(opts.dataset, L_train, L_val), file=f)
                        print('===========================================', file=f)
                        print('==============Best IoU=============', file=f)
                        print("Best Epoch: %d    Best itrs: %d" % (Best_epoch, Best_itrs), file=f)
                        print("Previous best IoU: %.5f" % Previous_best_IoU, file=f)
                        print('===================================', file=f)
                        print('==================== Previous Best Val Score================', file=f)
                        print(Now_Best_val_score, file=f)
                        print('============================================================', file=f)
                print()
                print("=============Best IoU=============")
                print("Best Epoch: %d    Best itrs: %d" % (Best_epoch, Best_itrs))
                print("Previous best IoU: %.5f" % Previous_best_IoU)
                print("==================================")
                print()
                if val_score['Mean IoU'] > best_score:  # save best model
                    best_score = val_score['Mean IoU']
                    #print("Best IoU", best_score)
                    save_ckpt(pth_file_name + '/best_%s_%s_os%d.pth' %
                              (opts.model, opts.dataset,opts.output_stride))

                if vis is not None:  # visualize validation score and samples
                    vis.vis_scalar("[Val] Overall Acc", cur_itrs, val_score['Overall Acc'])
                    vis.vis_scalar("[Val] Mean IoU", cur_itrs, val_score['Mean IoU'])
                    vis.vis_table("[Val] Class IoU", val_score['Class IoU'])

                    for k, (img, target, lbl) in enumerate(ret_samples):
                        img = (denorm(img) * 255).astype(np.uint8)
                        target = train_dst.decode_target(target).transpose(2, 0, 1).astype(np.uint8)
                        lbl = train_dst.decode_target(lbl).transpose(2, 0, 1).astype(np.uint8)
                        concat_img = np.concatenate((img, target, lbl), axis=2)  # concat along width
                        vis.vis_image('Sample %d' % k, concat_img)
                model.train()
            scheduler.step()  

            if cur_itrs >=  opts.total_itrs:
                # Best_val_score = Now_Best_val_score
                end = datetime.datetime.now()
                print("==================== Previous Best Val Score================")
                print(Now_Best_val_score)
                print("============================================================")
                print('===================训练时间===================')
                print('begin time:%s' % start)
                print('end time:%s' % end)
                print('训练用时:%s' % (end - start))
                print("=============================================")
                with open(pth_file_name + '/best_result.txt', 'w') as f:
                    print('Best Result: ', file=f)
                    print('================== 数据集 =================', file=f)
                    print("Dataset: %s, Train set: %d, Val set: %d" % (opts.dataset, L_train, L_val), file=f)
                    print('===========================================', file=f)
                    print('==============Best IoU=============', file=f)
                    print("Best Epoch: %d    Best itrs: %d" % (Best_epoch, Best_itrs), file=f)
                    print("Previous best IoU: %.5f" % Previous_best_IoU, file=f)
                    print('===================================', file=f)
                    print('==================== Previous Best Val Score================', file=f)
                    print(Now_Best_val_score, file=f)
                    print('============================================================', file=f)
                    print('===================训练时间===================', file=f)
                    print('begin time:%s' % start, file=f)
                    print('end time:%s' % end, file=f)
                    print('训练用时:%s' % (end - start), file=f)
                    print('==============================================', file=f)
                return

if __name__ == '__main__':
    main()


