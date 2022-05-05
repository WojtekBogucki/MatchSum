import numpy as np

import json
from os.path import join
import sys
import torch
import logging
import tempfile
import subprocess as sp
from datetime import timedelta
from time import time
from itertools import combinations

from pyrouge import Rouge155
from pyrouge.utils import log
from rouge import Rouge

from fastNLP.core.losses import LossBase
from fastNLP.core.metrics import MetricBase


try:  
   _ROUGE_PATH = os.environ["ROUGE_PATH"]
except KeyError: 
   print("Please set the environment variable ROUGE_PATH")
   sys.exit(1)
    

class MarginRankingLoss(LossBase):      
    
    def __init__(self, margin, score=None, summary_score=None):
        super(MarginRankingLoss, self).__init__()
        self._init_param_map(score=score, summary_score=summary_score)
        self.margin = margin
        self.loss_func = torch.nn.MarginRankingLoss(margin)

    def get_loss(self, score, summary_score):
        
        # equivalent to initializing TotalLoss to 0
        # here is to avoid that some special samples will not go into the following for loop
        ones = torch.ones(score.size()).cuda(score.device)
        loss_func = torch.nn.MarginRankingLoss(0.0)
        TotalLoss = loss_func(score, score, ones)

        # candidate loss
        n = score.size(1)
        for i in range(1, n):
            pos_score = score[:, :-i]
            neg_score = score[:, i:]
            pos_score = pos_score.contiguous().view(-1)
            neg_score = neg_score.contiguous().view(-1)
            ones = torch.ones(pos_score.size()).cuda(score.device)
            loss_func = torch.nn.MarginRankingLoss(self.margin * i)
            TotalLoss += loss_func(pos_score, neg_score, ones)

        # gold summary loss
        pos_score = summary_score.unsqueeze(-1).expand_as(score)
        neg_score = score
        pos_score = pos_score.contiguous().view(-1)
        neg_score = neg_score.contiguous().view(-1)
        ones = torch.ones(pos_score.size()).cuda(score.device)
        loss_func = torch.nn.MarginRankingLoss(0.0)
        TotalLoss += loss_func(pos_score, neg_score, ones)
        
        return TotalLoss

class ValidMetric(MetricBase):
    def __init__(self, save_path, data, score=None):
        super(ValidMetric, self).__init__()
        self._init_param_map(score=score)
 
        self.save_path = save_path
        self.data = data

        self.top1_correct = 0
        self.top6_correct = 0
        self.top10_correct = 0
         
        self.rouge = Rouge()
        self.ROUGE = 0.0
        self.Error = 0

        self.cur_idx = 0
    
    # an approximate method of calculating ROUGE
    def fast_rouge(self, dec, ref):
        if dec == '' or ref == '':
            return 0.0
        scores = self.rouge.get_scores(dec, ref)
        return (scores[0]['rouge-1']['f'] + scores[0]['rouge-2']['f'] + scores[0]['rouge-l']['f']) / 3

    def evaluate(self, score):
        batch_size = score.size(0)
        self.top1_correct += int(torch.sum(torch.max(score, dim=1).indices == 0))
        self.top6_correct += int(torch.sum(torch.max(score, dim=1).indices <= 5))
        self.top10_correct += int(torch.sum(torch.max(score, dim=1).indices <= 9))

        # Fast ROUGE
        for i in range(batch_size):
            max_idx = int(torch.max(score[i], dim=0).indices)
            if max_idx >= len(self.data[self.cur_idx]['indices']):
                self.Error += 1 # Check if the candidate summary generated by padding is selected
                self.cur_idx += 1
                continue
            ext_idx = self.data[self.cur_idx]['indices'][max_idx]
            ext_idx.sort()
            dec = []
            ref = ' '.join(self.data[self.cur_idx]['summary'])
            for j in ext_idx:
                dec.append(self.data[self.cur_idx]['text'][j])
            dec = ' '.join(dec)
            self.ROUGE += self.fast_rouge(dec, ref)
            self.cur_idx += 1

    def get_metric(self, reset=True):
        top1_accuracy = self.top1_correct / self.cur_idx
        top6_accuracy = self.top6_correct / self.cur_idx
        top10_accuracy = self.top10_correct / self.cur_idx
        ROUGE = self.ROUGE / self.cur_idx
        eval_result = {'top1_accuracy': top1_accuracy, 'top6_accuracy': top6_accuracy, 
                       'top10_accuracy': top10_accuracy, 'Error': self.Error, 'ROUGE': ROUGE}
        with open(join(self.save_path, 'train_info.txt'), 'a') as f:
            print('top1_accuracy = {}, top6_accuracy = {}, top10_accuracy = {}, Error = {}, ROUGE = {}'.format(
                  top1_accuracy, top6_accuracy, top10_accuracy, self.Error, ROUGE), file=f)
        if reset:
            self.top1_correct = 0
            self.top6_correct = 0
            self.top10_correct = 0
            self.ROUGE = 0.0
            self.Error = 0
            self.cur_idx = 0
        return eval_result
        
class MatchRougeMetric(MetricBase):
    def __init__(self, data, dec_path, ref_path, n_total, score=None):
        super(MatchRougeMetric, self).__init__()
        self._init_param_map(score=score)
        self.data        = data
        self.dec_path    = dec_path
        self.ref_path    = ref_path
        self.n_total     = n_total
        self.cur_idx = 0
        self.ext = []
        self.start = time()

    
    def evaluate(self, score):
        ext = int(torch.max(score, dim=1).indices) # batch_size = 1
        self.ext.append(ext)
        self.cur_idx += 1
        print('{}/{} ({:.2f}%) decoded in {} seconds\r'.format(
              self.cur_idx, self.n_total, self.cur_idx/self.n_total*100, timedelta(seconds=int(time()-self.start))
             ), end='')
    
    def get_metric(self, reset=True):
        
        print('\nStart writing files !!!')
        for i, ext in enumerate(self.ext):
            sent_ids = self.data[i]['indices'][ext]
            dec, ref = [], []
            
            for j in sent_ids:
                dec.append(self.data[i]['text'][j])
            for sent in self.data[i]['summary']:
                ref.append(sent)

            with open(join(self.dec_path, '{}.dec'.format(i)), 'w') as f:
                for sent in dec:
                    print(sent, file=f)
            with open(join(self.ref_path, '{}.ref'.format(i)), 'w') as f:
                for sent in ref:
                    print(sent, file=f)
        
        print('Start evaluating ROUGE score !!!')
        R_1, R_2, R_L = MatchRougeMetric.eval_rouge(self.dec_path, self.ref_path)
        eval_result = {'ROUGE-1': R_1, 'ROUGE-2': R_2, 'ROUGE-L':R_L}

        if reset == True:
            self.cur_idx = 0
            self.ext = []
            self.data = []
            self.start = time()
        return eval_result
        
    @staticmethod
    def eval_rouge(dec_dir, ref_dir, Print=True):
        assert _ROUGE_PATH is not None
        log.get_global_console_logger().setLevel(logging.WARNING)
        dec_pattern = '(\d+).dec'
        ref_pattern = '#ID#.ref'
        cmd = '-c 95 -r 1000 -n 2 -m'
        with tempfile.TemporaryDirectory() as tmp_dir:
            Rouge155.convert_summaries_to_rouge_format(
                dec_dir, join(tmp_dir, 'dec'))
            Rouge155.convert_summaries_to_rouge_format(
                ref_dir, join(tmp_dir, 'ref'))
            Rouge155.write_config_static(
                join(tmp_dir, 'dec'), dec_pattern,
                join(tmp_dir, 'ref'), ref_pattern,
                join(tmp_dir, 'settings.xml'), system_id=1
            )
            cmd = (join(_ROUGE_PATH, 'ROUGE-1.5.5.pl')
                + ' -e {} '.format(join(_ROUGE_PATH, 'data'))
                + cmd
                + ' -a {}'.format(join(tmp_dir, 'settings.xml')))
            output = sp.check_output(cmd.split(' '), universal_newlines=True)
            R_1 = float(output.split('\n')[3].split(' ')[3])
            R_2 = float(output.split('\n')[7].split(' ')[3])
            R_L = float(output.split('\n')[11].split(' ')[3])
            print(output)
        if Print is True:
            rouge_path = join(dec_dir, '../ROUGE.txt')
            with open(rouge_path, 'w') as f:
                print(output, file=f)
        return R_1, R_2, R_L
    
