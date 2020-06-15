"""
Defines the optimizer and the network outputs
"""
#!/usr/bin/env python
# coding=utf8

import os
from os.path import exists
import sys
from collections import OrderedDict
import numpy as np
from copy import deepcopy
import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')

PARL_DIR = os.environ.get('PARL_DIR', '')
assert exists(PARL_DIR), ('PARL_DIR', PARL_DIR, 'not exists')
sys.path.insert(0, PARL_DIR)

from paddle.fluid.executor import _fetch_var
from paddle import fluid
import parl.layers as layers
from parl.framework.algorithm import Algorithm

from utils import get_boundaries_and_values_for_piecewise_decay
from fluid_utils import fluid_sequence_delay


class RLAlgorithm(Algorithm):
    """
    For generation tasks
    return:
        {
            feed_names: list of ordered feed names, optional.
            fetch_dict: orderedDict of fetch vars
        }
    """
    def __init__(self, model, optimizer, lr=None, 
                    hyperparas=None, gpu_id=-1,
                    gamma=None, target_update_ratio=0.01,
                    Q_type=False):
        hyperparas = {} if hyperparas is None else hyperparas
        super(RLAlgorithm, self).__init__(model, hyperparas=hyperparas, gpu_id=gpu_id)
        self.target_model = deepcopy(model)

        self.optimizer = optimizer
        self.lr = lr
        self._gamma = gamma
        self._Q_type = Q_type
        self.target_update_ratio = target_update_ratio
        self._learn_cnt = 0

        self.gpu_id = gpu_id
        self._safe_eps = 1e-5
        self._reward_scale = 0.01

    def get_target_Q(self, inputs, rewards):
        if self._Q_type == 'Q_learning':
            target_output_dict = self.target_model.forward(inputs, output_type='max_Q')
            next_Q = target_output_dict['Q']
        elif self._Q_type == 'SARSA':
            target_output_dict = self.target_model.forward(inputs, output_type='c_Q')
            next_Q = target_output_dict['Q']
        elif self._Q_type == 'double_Q':
            max_id = self.model.forward(inputs, output_type='double_Q', double_type='max_id')['Q']
            next_Q = self.target_model.forward(inputs, output_type='double_Q', double_type='double_Q', double_id=max_id)['Q']
        next_Q_delay = fluid_sequence_delay(rewards * 0 + next_Q, OOV=0)    # TODO, use "rewards * 0" to recover lod_level in infer stage
        target_Q = rewards + self._gamma * next_Q_delay
        return target_Q

    def train(self):
        """train"""
        inputs = self.model.create_inputs(mode='train')
        click_id = layers.cast(inputs['click_id'], 'float32') * self._reward_scale

        output_dict = self.model.forward(inputs, output_type='c_Q')
        c_Q = output_dict['Q']
        target_Q = self.get_target_Q(inputs, click_id)
        target_Q.stop_gradient = True
        loss = layers.reduce_mean(layers.square_error_cost(c_Q, target_Q))

        if self.optimizer == 'Adam':
            optimizer = fluid.optimizer.Adam(learning_rate=self.lr, epsilon=1e-4)
        elif self.optimizer == 'SGD':
            optimizer = fluid.optimizer.SGD(learning_rate=self.lr)
        optimizer.minimize(loss)

        fetch_dict = OrderedDict()
        fetch_dict['loss'] = loss             # don't rename 'loss', which will be used in parallel exe in computational task
        fetch_dict['c_Q'] = c_Q / self._reward_scale
        fetch_dict['click_id'] = click_id / self._reward_scale
        return {'fetch_dict': fetch_dict}

    def test(self):
        """test"""
        inputs = self.model.create_inputs(mode='train')
        click_id = layers.cast(inputs['click_id'], 'float32') * self._reward_scale

        output_dict = self.model.forward(inputs, output_type='c_Q')
        c_Q = output_dict['Q']
        target_Q = self.get_target_Q(inputs, click_id)
        loss = layers.reduce_mean(layers.square_error_cost(c_Q, target_Q))

        fetch_dict = OrderedDict()
        fetch_dict['loss'] = loss
        fetch_dict['c_Q'] = c_Q / self._reward_scale
        fetch_dict['click_id'] = click_id / self._reward_scale
        return {'fetch_dict': fetch_dict}

    def inference(self):
        """inference"""
        inputs = self.model.create_inputs(mode='inference')
        output_dict = self.model.forward(inputs, output_type='c_Q')

        fetch_dict = OrderedDict()
        fetch_dict['c_Q'] = output_dict['Q'] / self._reward_scale
        return {'fetch_dict': fetch_dict}

    def infer_init(self):
        """inference only the init part"""
        inputs = self.model.create_inputs(mode='infer_init')
        output_dict = self.model.infer_init(inputs)

        fetch_dict = OrderedDict()
        fetch_dict['prev_hidden'] = output_dict['user_feature']
        return {'feed_names': inputs.keys(),
                'fetch_dict': fetch_dict}

    def infer_onestep(self):
        """inference the gru-unit by one step"""
        inputs = self.model.create_inputs(mode='infer_onestep')
        output_dict = self.model.infer_onestep(inputs)
        # click_prob = layers.slice(click_prob, axes=[1], starts=[1], ends=[2])

        fetch_dict = OrderedDict()
        fetch_dict['prev_hidden'] = output_dict['hidden']
        fetch_dict['c_Q'] = output_dict['Q'] / self._reward_scale
        return {'feed_names': inputs.keys(),
                'fetch_dict': fetch_dict}

    def before_every_batch(self):
        """
        TODO: memory leak caused by np.array(var.get_tensor()) within _fetch_var() 
            (https://github.com/PaddlePaddle/Paddle/issues/17176)
        """
        interval = 20
        if self._learn_cnt % interval == 0:
            self.model.sync_paras_to(self.target_model, self.gpu_id, 1.0)
        self._learn_cnt += 1

        # if self._learn_cnt == 0:
        #     self.model.sync_paras_to(self.target_model, self.gpu_id, 1.0)
        #     self._learn_cnt += 1
        #     return    

        # self.model.sync_paras_to(self.target_model, self.gpu_id, self.target_update_ratio)
        # self._learn_cnt += 1  



