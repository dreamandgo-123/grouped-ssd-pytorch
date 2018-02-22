import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
from layers import *
from data import v2_custom
import os

GROUPS_VGG = 1
GROUPS_EXTRA = 1

class SSD(nn.Module):
    """Single Shot Multibox Architecture
    The network is composed of a base VGG network followed by the
    added multibox conv layers.  Each multibox layer branches into
        1) conv2d for class conf scores
        2) conv2d for localization predictions
        3) associated priorbox layer to produce default bounding
           boxes specific to the layer's feature map size.
    See: https://arxiv.org/pdf/1512.02325.pdf for more details.

    Args:
        phase: (string) Can be "test" or "train"
        base: VGG16 layers for input, size of either 300 or 500
        extras: extra layers that feed to multibox loc and conf layers
        head: "multibox head" consists of loc and conf conv layers
    """

    def __init__(self, phase, base, extras, head, num_classes, batch_norm):
        super(SSD, self).__init__()
        self.phase = phase
        self.num_classes = num_classes
        self.batch_norm = batch_norm
        # TODO: implement __call__ in PriorBox
        self.priorbox = PriorBox(v2_custom)
        self.priors = Variable(self.priorbox.forward(), volatile=True)
        self.size = 300

        # SSD network
        self.vgg = nn.ModuleList(base)

        # Layer learns to scale the l2 normalized features from conv4_3
        self.L2Norm = L2Norm(512, 20)
        self.extras = nn.ModuleList(extras)

        self.loc = nn.ModuleList(head[0])
        self.conf = nn.ModuleList(head[1])

        if phase == 'test':
            self.softmax = nn.Softmax(dim=-1)
            self.detect = Detect(num_classes, 0, 200, 0.01, 0.45)

        # feature fuse layers
        # fuse conv4_3 and conv5_3 feature map for improved small object detection
        # layer for deconv of conv5_3 to match dim of conv4_3
        self.fuse_deconv_53 = nn.ConvTranspose2d(512, 512,
                                                 kernel_size=2, stride=2)
        if batch_norm:
            self.bn_fuse_deconv_53 = nn.BatchNorm2d(512)
        # init the deconv layer with bilinear upsampling
        # TODO: how?

        self.fuse_conv_53 = nn.Conv2d(512, 512, kernel_size=3, padding=1)
        if batch_norm:
            self.bn_fuse_conv_53 = nn.BatchNorm2d(512)
        # L2 norm for fuse_conv_53
        self.L2Norm_53 = L2Norm(512, 10)

        # extra conv for conf4_3 for efficient fusing of fuse_conv5_3
        self.fuse_conv_43 = nn.Conv2d(512, 512, kernel_size=3, padding=1)
        if batch_norm:
            self.bn_fuse_conv_43 = nn.BatchNorm2d(512)


    def forward(self, x):
        """Applies network layers and ops on input image(s) x.

        Args:
            x: input image or batch of images. Shape: [batch,3*batch,300,300].

        Return:
            Depending on phase:
            test:
                Variable(tensor) of output class label predictions,
                confidence score, and corresponding location predictions for
                each object detected. Shape: [batch,topk,7]

            train:
                list of concat outputs from:
                    1: confidence layers, Shape: [batch*num_priors,num_classes]
                    2: localization layers, Shape: [batch,num_priors*4]
                    3: priorbox layers, Shape: [2,num_priors*4]
        """
        sources = list()
        loc = list()
        conf = list()

        # apply vgg up to conv4_3 relu
        # TODO: change hardcoding 23 for BN case
        if self.batch_norm is False:
            idx_until_conv4_3 = 23
        elif self.batch_norm is True:
            idx_until_conv4_3 = 33
        for k in range(idx_until_conv4_3):
            x = self.vgg[k](x)
        x_conv43 = x

        # apply vgg up to conv4_3 relu
        if self.batch_norm is False:
            idx_until_conv5_3 = 30
        elif self.batch_norm is True:
            idx_until_conv5_3 = 43
        for k in range(idx_until_conv4_3, idx_until_conv5_3 - 1):
            x = self.vgg[k](x)
        x_conv53 = self.vgg[idx_until_conv5_3](x)

        # now x_conv43 is conv_43 and x_conv53 is conv_53

        # apply extra fusion conv at conv4_3
        fuse_conv43 = self.fuse_conv_43(x_conv43)
        if self.batch_norm:
            fuse_conv43 = self.bn_fuse_conv_43(fuse_conv43)

        # apply deconv & extra fusion conv at conv5_3
        fuse_deconv53 = self.fuse_deconv_53(x_conv53)
        if self.batch_norm:
            fuse_deconv53 = self.bn_fuse_deconv_53(fuse_deconv53)
        fuse_conv53 = self.fuse_conv_53(fuse_deconv53)
        if self.batch_norm:
            fuse_conv53 = self.bn_fuse_deconv_53(fuse_conv53)

        # apply L2norm at each fused convs
        l2_fuse_conv43 = self.L2Norm(fuse_conv43)
        l2_fuse_conv53 = self.L2Norm_53(fuse_conv53)

        # apply sum and final relu to create source
        s = F.relu(l2_fuse_conv43 + l2_fuse_conv53, inplace=True)

        # TODO: append lower level features
        sources.append(s)

        # fuse done, keep forward back from conv5_3
        x = x_conv53

        # apply vgg up to fc7
        for k in range(idx_until_conv5_3, len(self.vgg)):
            x = self.vgg[k](x)
        sources.append(x)

        # apply extra layers and cache source layer outputs
        # hard-coded for BN case
        if self.batch_norm is False:
            for k, v in enumerate(self.extras):
                x = F.relu(v(x), inplace=True)
                if k % 2 == 1:
                    sources.append(x)
        elif self.batch_norm is True:
            for k, v in enumerate(self.extras):
                x = v(x)
                if k % 2 == 1:
                    x = F.relu(x, inplace=True)
                if k % 4 == 3:
                    sources.append(x)

        # apply multibox head to source layers
        for (x, l, c) in zip(sources, self.loc, self.conf):
            loc.append(l(x).permute(0, 2, 3, 1).contiguous())
            conf.append(c(x).permute(0, 2, 3, 1).contiguous())

        loc = torch.cat([o.view(o.size(0), -1) for o in loc], 1)
        conf = torch.cat([o.view(o.size(0), -1) for o in conf], 1)
        if self.phase == "test":
            output = self.detect(
                loc.view(loc.size(0), -1, 4),                   # loc preds
                #self.softmax(conf.view(-1, self.num_classes)),  # conf preds
                self.softmax(conf.view(conf.size(0), -1, self.num_classes)),
                self.priors.type(type(x.data))                  # default boxes
            )
        else:
            output = (
                loc.view(loc.size(0), -1, 4),
                conf.view(conf.size(0), -1, self.num_classes),
                self.priors
            )
        return output

    def load_weights(self, base_file):
        other, ext = os.path.splitext(base_file)
        if ext == '.pkl' or '.pth':
            print('Loading weights into state dict...')
            weight_pretrained = torch.load(base_file, map_location=lambda storage, loc: storage)
            self.load_state_dict(weight_pretrained)
            print('Finished!')
        else:
            print('Sorry only .pth and .pkl files supported.')


# This function is derived from torchvision VGG make_layers()
# https://github.com/pytorch/vision/blob/master/torchvision/models/vgg.py
def vgg(cfg, i, batch_norm=False):
    layers = []
    in_channels = i
    for v in cfg:
        if v == 'M':
            layers += [nn.MaxPool2d(kernel_size=2, stride=2)]
        elif v == 'C':
            layers += [nn.MaxPool2d(kernel_size=2, stride=2, ceil_mode=True)]
        else:
            # depthwise separable conv: add groups=4 (4 phases)
            conv2d = nn.Conv2d(in_channels, v, kernel_size=3, padding=1, groups=GROUPS_VGG)
            if batch_norm:
                layers += [conv2d, nn.BatchNorm2d(v), nn.ReLU(inplace=True)]
            else:
                layers += [conv2d, nn.ReLU(inplace=True)]
            in_channels = v
    pool5 = nn.MaxPool2d(kernel_size=3, stride=1, padding=1)
    conv6 = nn.Conv2d(512, 1024, kernel_size=3, padding=6, dilation=6, groups=GROUPS_VGG)
    conv7 = nn.Conv2d(1024, 1024, kernel_size=1, groups=GROUPS_VGG)
    if batch_norm:
        layers += [pool5,
                   conv6, nn.BatchNorm2d(1024), nn.ReLU(inplace=True),
                   conv7, nn.BatchNorm2d(1024), nn.ReLU(inplace=True)]
    else:
        layers += [pool5, conv6,
                   nn.ReLU(inplace=True), conv7, nn.ReLU(inplace=True)]
    return layers


def add_extras(cfg, i, batch_norm=False):
    # Extra layers added to VGG for feature scaling
    layers = []
    in_channels = i
    flag = False
    for k, v in enumerate(cfg):
        if in_channels != 'S':
            if v == 'S':
                if batch_norm is False:
                    layers += [nn.Conv2d(in_channels, cfg[k + 1],
                           kernel_size=(1, 3)[flag], stride=2, padding=1, groups=GROUPS_EXTRA)]
                else:
                    layers += [nn.Conv2d(in_channels, cfg[k + 1],
                                         kernel_size=(1, 3)[flag], stride=2, padding=1, groups=GROUPS_EXTRA),
                               nn.BatchNorm2d(cfg[k + 1])]

            else:
                if batch_norm is False:
                    layers += [nn.Conv2d(in_channels, v, kernel_size=(1, 3)[flag], groups=GROUPS_EXTRA)]
                else:
                    layers += [nn.Conv2d(in_channels, v, kernel_size=(1, 3)[flag], groups=GROUPS_EXTRA),
                               nn.BatchNorm2d(v)]
            flag = not flag
        in_channels = v
    return layers


def multibox(vgg, extra_layers, cfg, num_classes, batch_norm):
    loc_layers = []
    conf_layers = []
    # hard-coded
    # TODO: make this generic
    if batch_norm is False:
        vgg_source = [21, -2]
    elif batch_norm is True:
        vgg_source = [30, -3]
    for k, v in enumerate(vgg_source):
        loc_layers += [nn.Conv2d(vgg[v].out_channels,
                                 cfg[k] * 4, kernel_size=3, padding=1)]
        conf_layers += [nn.Conv2d(vgg[v].out_channels,
                        cfg[k] * num_classes, kernel_size=3, padding=1)]
    # hard-coded
    if batch_norm is False:
        for k, v in enumerate(extra_layers[1::2], 2):
            loc_layers += [nn.Conv2d(v.out_channels, cfg[k]
                                     * 4, kernel_size=3, padding=1)]
            conf_layers += [nn.Conv2d(v.out_channels, cfg[k]
                                      * num_classes, kernel_size=3, padding=1)]
    elif batch_norm is True:
        for k, v in enumerate(extra_layers[2::4], 2):
            loc_layers += [nn.Conv2d(v.out_channels, cfg[k]
                                     * 4, kernel_size=3, padding=1)]
            conf_layers += [nn.Conv2d(v.out_channels, cfg[k]
                                      * num_classes, kernel_size=3, padding=1)]
    return vgg, extra_layers, (loc_layers, conf_layers)


base = {
    '300': [64, 64, 'M', 128, 128, 'M', 256, 256, 256, 'C', 512, 512, 512, 'M',
    512, 512, 512],
    #'300': [128, 128, 'M', 256, 256, 'M', 512, 512, 512, 'C', 1024, 1024, 1024, 'M',
    #1024, 1024, 1024],
    '512': [],
}
extras = {
    '300': [512, 'S', 1024, 256, 'S', 512, 256, 512, 256, 512],
    '512': [],
}
mbox = {
    #'300': [4, 6, 6, 6, 4, 4],  # number of boxes per feature map location
    # for v2_custom cfg: use 6 for lowest layer
    '300': [6, 6, 6, 6, 4, 4],
    '512': [],
}


def build_ssd(phase, size=300, num_classes=21, batch_norm=False):
    if phase != "test" and phase != "train":
        print("Error: Phase not recognized")
        return
    if size != 300:
        print("Error: Sorry only SSD300 is supported currently!")
        return

    # change the input channel from i=3 to 12
    return SSD(phase, *multibox(vgg(base[str(size)], i=12, batch_norm=batch_norm),
                                add_extras(extras[str(size)], 1024, batch_norm),
                                mbox[str(size)], num_classes, batch_norm), num_classes, batch_norm)