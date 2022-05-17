# -*- coding: utf-8 -*-
from __future__ import print_function, division

import copy
import os
import sys
import time
import random
import scipy
import torch
import torchvision
import torchvision.transforms as transforms
import numpy as np
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
import torch.backends.cudnn as cudnn

from scipy import special
from datetime import datetime
from tensorboardX import SummaryWriter
from pymic.io.image_read_write import save_nd_array_as_image
from pymic.io.nifty_dataset import NiftyDataset
from pymic.transform.trans_dict import TransformDict
from pymic.net.net_dict_seg import SegNetDict
from pymic.net_run.agent_abstract import NetRunAgent
from pymic.net_run.agent_seg import SegmentationAgent
from pymic.net_run.infer_func import Inferer
from pymic.loss.loss_dict_seg import SegLossDict
from pymic.loss.seg.combined import CombinedLoss
from pymic.loss.seg.util import get_soft_label
from pymic.loss.seg.util import reshape_prediction_and_ground_truth
from pymic.loss.seg.util import get_classwise_dice
from pymic.util.image_process import convert_label
from pymic.util.parse_config import parse_config
from pymic.loss.seg.ssl import EntropyLoss


class SSLSegAgent(SegmentationAgent):
    """
    Training and testing agent for semi-supervised segmentation
    """
    def __init__(self, config, stage = 'train'):
        super(SSLSegAgent, self).__init__(config, stage)
        self.transform_dict  = TransformDict
        self.train_set_unlab = None 

    def get_unlabeled_dataset_from_config(self):
        root_dir  = self.config['dataset']['root_dir']
        modal_num = self.config['dataset']['modal_num']
        transform_names = self.config['dataset']['train_transform_unlab']
        
        self.transform_list  = []
        if(transform_names is None or len(transform_names) == 0):
            data_transform = None 
        else:
            transform_param = self.config['dataset']
            transform_param['task'] = 'segmentation' 
            for name in transform_names:
                if(name not in self.transform_dict):
                    raise(ValueError("Undefined transform {0:}".format(name))) 
                one_transform = self.transform_dict[name](transform_param)
                self.transform_list.append(one_transform)
            data_transform = transforms.Compose(self.transform_list)

        csv_file = self.config['dataset'].get('train_csv_unlab', None)
        dataset  = NiftyDataset(root_dir=root_dir,
                                csv_file  = csv_file,
                                modal_num = modal_num,
                                with_label= False,
                                transform = data_transform )
        return dataset

    def create_dataset(self):
        super(SSLSegAgent, self).create_dataset()
        if(self.stage == 'train'):
            if(self.train_set_unlab is None):
                self.train_set_unlab = self.get_unlabeled_dataset_from_config()
            if(self.deterministic):
                def worker_init_fn(worker_id):
                    random.seed(self.random_seed+worker_id)
                worker_init = worker_init_fn
            else:
                worker_init = None

            bn_train_unlab = self.config['dataset']['train_batch_size_unlab']
            num_worker = self.config['dataset'].get('num_workder', 16)
            self.train_loader_unlab = torch.utils.data.DataLoader(self.train_set_unlab, 
                batch_size = bn_train_unlab, shuffle=True, num_workers= num_worker,
                worker_init_fn=worker_init)

    def get_consistency_weight_with_rampup(self, w0, current, rampup_length):
        """
        Get conssitency weight with rampup https://arxiv.org/abs/1610.02242
        """
        if(rampup_length is None or rampup_length == 0):
            return w0
        else:
            current = np.clip(current, 0, rampup_length)
            phase   = 1.0 - (current + 0.0) / rampup_length
            return  w0*np.exp(-5.0 * phase * phase)

    def training(self):
        class_num   = self.config['network']['class_num']
        iter_valid  = self.config['training']['iter_valid']
        ssl_cfg     = self.config['semi_supervised_learning']
        train_loss  = 0
        train_loss_sup = 0
        train_loss_unsup = 0
        train_dice_list = []
        self.net.train()
        for it in range(iter_valid):
            try:
                data_lab = next(self.trainIter)
            except StopIteration:
                self.trainIter = iter(self.train_loader)
                data_lab = next(self.trainIter)
            try:
                data_unlab = next(self.trainIter_unlab)
            except StopIteration:
                self.trainIter_unlab = iter(self.train_loader_unlab)
                data_unlab = next(self.trainIter_unlab)

            # get the inputs
            x0   = self.convert_tensor_type(data_lab['image'])
            y0   = self.convert_tensor_type(data_lab['label_prob'])  
            x1   = self.convert_tensor_type(data_unlab['image'])
            inputs = torch.cat([x0, x1], dim = 0)               
            
            # # for debug
            # for i in range(inputs.shape[0]):
            #     image_i = inputs[i][0]
            #     label_i = labels_prob[i][1]
            #     pixw_i  = pix_w[i][0]
            #     print(image_i.shape, label_i.shape, pixw_i.shape)
            #     image_name = "temp/image_{0:}_{1:}.nii.gz".format(it, i)
            #     label_name = "temp/label_{0:}_{1:}.nii.gz".format(it, i)
            #     weight_name= "temp/weight_{0:}_{1:}.nii.gz".format(it, i)
            #     save_nd_array_as_image(image_i, image_name, reference_name = None)
            #     save_nd_array_as_image(label_i, label_name, reference_name = None)
            #     save_nd_array_as_image(pixw_i, weight_name, reference_name = None)
            # continue

            inputs, y0 = inputs.to(self.device), y0.to(self.device)
            
            # zero the parameter gradients
            self.optimizer.zero_grad()
                
            # forward + backward + optimize
            outputs = self.net(inputs)
            n0 = list(x0.shape)[0] 
            p0, p1  = torch.tensor_split(outputs, [n0,], dim = 0)
            loss_sup = self.get_loss_value(data_lab, x0, p0, y0)
            loss_dict = {"prediction":outputs, 'softmax':True}
            loss_unsup = EntropyLoss()(loss_dict)

            consis_w0= ssl_cfg.get('consis_w', 0.1)
            iter_sup = ssl_cfg.get('iter_sup', 0)
            iter_max = self.config['training']['iter_max']
            ramp_up_length = ssl_cfg.get('ramp_up_length', iter_max)
            consis_w = self.get_consistency_weight_with_rampup(
                consis_w0, self.glob_it, ramp_up_length)
            consis_w = 0.0 if self.glob_it < iter_sup else consis_w
            loss = loss_sup + consis_w*loss_unsup
            # if (self.config['training']['use'])
            loss.backward()
            self.optimizer.step()
            self.scheduler.step()

            train_loss = train_loss + loss.item()
            train_loss_sup   = train_loss_sup + loss_sup.item()
            train_loss_unsup = train_loss_unsup + loss_unsup.item() 
            # get dice evaluation for each class in annotated images
            if(isinstance(p0, tuple) or isinstance(p0, list)):
                p0 = p0[0] 
            p0_argmax = torch.argmax(p0, dim = 1, keepdim = True)
            p0_soft   = get_soft_label(p0_argmax, class_num, self.tensor_type)
            p0_soft, y0 = reshape_prediction_and_ground_truth(p0_soft, y0) 
            dice_list   = get_classwise_dice(p0_soft, y0)
            train_dice_list.append(dice_list.cpu().numpy())
        train_avg_loss = train_loss / iter_valid
        train_avg_loss_sup = train_loss_sup / iter_valid
        train_avg_loss_unsup = train_loss_unsup / iter_valid
        train_cls_dice = np.asarray(train_dice_list).mean(axis = 0)
        train_avg_dice = train_cls_dice.mean()

        train_scalers = {'loss': train_avg_loss, 'loss_sup':train_avg_loss_sup,
            'loss_unsup':train_avg_loss_unsup, 'consis_w':consis_w,
            'avg_dice':train_avg_dice,     'class_dice': train_cls_dice}
        return train_scalers
        
    def write_scalars(self, train_scalars, valid_scalars, glob_it):
        loss_scalar ={'train':train_scalars['loss'], 
                      'valid':valid_scalars['loss']}
        loss_sup_scalar  = {'train':train_scalars['loss_sup']}
        loss_upsup_scalar  = {'train':train_scalars['loss_unsup']}
        dice_scalar ={'train':train_scalars['avg_dice'], 'valid':valid_scalars['avg_dice']}
        self.summ_writer.add_scalars('loss', loss_scalar, glob_it)
        self.summ_writer.add_scalars('loss_sup', loss_sup_scalar, glob_it)
        self.summ_writer.add_scalars('loss_unsup', loss_upsup_scalar, glob_it)
        self.summ_writer.add_scalars('consis_w', {'consis_w':train_scalars['consis_w']}, glob_it)
        self.summ_writer.add_scalars('dice', dice_scalar, glob_it)
        class_num = self.config['network']['class_num']
        for c in range(class_num):
            cls_dice_scalar = {'train':train_scalars['class_dice'][c], \
                'valid':valid_scalars['class_dice'][c]}
            self.summ_writer.add_scalars('class_{0:}_dice'.format(c), cls_dice_scalar, glob_it)
       
        print('train loss {0:.4f}, avg dice {1:.4f}'.format(
            train_scalars['loss'], train_scalars['avg_dice']), train_scalars['class_dice'])        
        print('valid loss {0:.4f}, avg dice {1:.4f}'.format(
            valid_scalars['loss'], valid_scalars['avg_dice']), valid_scalars['class_dice'])  

    def train_valid(self):
        self.trainIter_unlab = iter(self.train_loader_unlab)   
        super(SSLSegAgent, self).train_valid()    

def main():
    if(len(sys.argv) < 3):
        print('Number of arguments should be 3. e.g.')
        print('   pymic_net_run train config.cfg')
        exit()
    stage    = str(sys.argv[1])
    cfg_file = str(sys.argv[2])
    config   = parse_config(cfg_file)
    agent = SSLSegAgent(config, stage)
    agent.run()

if __name__ == "__main__":
    main()