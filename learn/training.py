"""
    Main training code. Loads data, builds the model, trains, tests, evaluates, writes outputs, etc.
"""
import torch
import torch.optim as optim
from torch.utils.data import DataLoader

import csv
import argparse
import os 
import numpy as np
import random
import sys
import time
from tqdm import tqdm
from collections import defaultdict

from constants import *
from logger import Tensorboard
import datasets
import evaluation
import interpret
import persistence
import learn.models as models
import learn.tools as tools

num_workers = 2

def main(args, reporter=None):
    start = time.time()
    args, model, optimizer, params, dicts = init(args)

    global MimicDataset
    global collate
    if args.model == 'hier_conv_attn':
        from datasets import MimicDatasetSentences as MimicDataset
        from datasets import collate_sentences as collate
    else:
        from datasets import MimicDataset
        from datasets import collate
    
    epochs_trained, metrics_hist_test = train_epochs(args, model, optimizer, params, dicts)
    print("TOTAL ELAPSED TIME FOR {} MODEL AND {} EPOCHS: {}".format(args.model, epochs_trained, round(time.time() - start)))
    return metrics_hist_test

def init(args):
    """
        Load data, build model, create optimizer, create vars to hold metrics, etc.
    """
    #need to handle really large text fields
    csv.field_size_limit(sys.maxsize)

    #load vocab and other lookups
    print("loading lookups...")

    dicts = datasets.load_lookups(args, hier=args.hier)

    model = tools.pick_model(args, dicts)
        
    print(model)

    if not args.test_model and not args.model == 'dummy':
        optimizer = optim.Adam(model.parameters(), weight_decay=args.weight_decay, lr=args.lr)
    else:
        optimizer = None

    #params = tools.make_param_dict(args)
    params = vars(args)
    
    return args, model, optimizer, params, dicts

def train_epochs(args, model, optimizer, params, dicts):
    """
        Main loop. does train and test
    """
    metrics_hist_train = defaultdict(lambda: [])
    metrics_hist_dev = defaultdict(lambda: [])
    metrics_hist_test = defaultdict(lambda: [])
    
    test_only = args.test_model is not None

    num_labels_fine = len(dicts['ind2c'])
    num_labels_coarse = len(dicts['ind2c_coarse'])

    epoch = 0
    
    if not test_only:
        dataset_train = MimicDataset(args.data_path, dicts, num_labels_fine, num_labels_coarse, args.max_len)
        dataset_dev = MimicDataset(args.data_path.replace('train', 'dev'), dicts, num_labels_fine, num_labels_coarse, args.max_len)
        model_dir = os.path.join(MODEL_DIR, '_'.join([args.model, time.strftime('%Y-%m-%d_%H:%M:%S')]))
        os.mkdir(model_dir)
    else:
        model_dir = os.path.dirname(os.path.abspath(args.test_model))

    dataset_test = MimicDataset(args.data_path.replace('train', 'test'), dicts, num_labels_fine, num_labels_coarse, args.max_len)
    tensorboard = Tensorboard(model_dir)
    
    #train for n_epochs unless criterion metric does not improve for [patience] epochs
    for epoch in range(args.n_epochs if not test_only else 0):
   
        losses = train(model, optimizer, args.Y, epoch, args.batch_size, args.embed_desc, dataset_train, args.shuffle, args.gpu, args.version, dicts, args.quiet)
        loss = np.mean(losses)
        print("epoch loss: " + str(loss))

        metrics_train = {'loss': loss}

        fold = 'test' if args.version == 'mimic2' else 'dev'

        #evaluate on dev
        with torch.no_grad():
            metrics_dev, _, _, _ = test(model, args.Y, epoch, dataset_dev, args.batch_size, args.embed_desc, fold, args.gpu, args.version, dicts, args.samples, model_dir)

        for name, val in metrics_train.items():
            tensorboard.log_scalar('%s_train' % (name), val, epoch)
            metrics_hist_train[name].append(metrics_train[name])
            metrics_hist_train.update({'epochs': epoch+1})
        for name, val in metrics_dev.items():
            tensorboard.log_scalar('%s_dev' % (name), val, epoch)
            metrics_hist_dev[name].append(metrics_dev[name])

        metrics_hist_all = (metrics_hist_train, metrics_hist_dev, None)

        #save metrics, model, params
        persistence.save_everything(args, dicts, metrics_hist_all, model, model_dir, params, args.criterion, evaluate=False, test_only=False)

        if args.criterion is not None:
            if early_stop(metrics_hist_dev, args.criterion, args.patience):
                #stop training, evaluate on test set and then stop the script
                print("%s hasn't improved in %d epochs, early stopping..." % (args.criterion, args.patience))
                break

    fold = 'test'            
    print("\nevaluating on test")
    with torch.no_grad():
        metrics_test, metrics_codes, metrics_inst, hadm_ids = test(model, args.Y, epoch, dataset_test, args.batch_size, args.embed_desc,fold, args.gpu, args.version, dicts, args.samples, model_dir)
    
    for name, val in metrics_test.items():
        if not test_only:
            tensorboard.log_scalar('%s_test' % (name), val, epoch)
        metrics_hist_test[name].append(metrics_test[name])
    
    metrics_hist_all = (metrics_hist_train, metrics_hist_dev, metrics_hist_test)

    tensorboard.close()
        
    #save metrics, model, params
    persistence.save_everything(args, dicts, metrics_hist_all, model, model_dir, params, args.criterion, metrics_codes=metrics_codes, metrics_inst=metrics_inst, hadm_ids=hadm_ids, evaluate=True, test_only=test_only)

    return epoch+1, metrics_hist_test

def early_stop(metrics_hist, criterion, patience):
    if not np.all(np.isnan(metrics_hist[criterion])):
        if criterion == 'loss-dev': 
            return np.nanargmin(metrics_hist[criterion]) > len(metrics_hist[criterion]) - patience
        else:
            return np.nanargmax(metrics_hist[criterion]) < len(metrics_hist[criterion]) - patience
    else:
        #keep training if criterion results have all been nan so far
        return False

def train(model, optimizer, Y, epoch, batch_size, embed_desc, dataset, shuffle, gpu, version, dicts, quiet):
    """
        Training loop.
        output: losses for each example for this iteration
    """
    print("EPOCH %d" % epoch)
    
    #accumulation_steps = batch_size/8
    #assert batch_size % 8 == 0
    #optimizer.zero_grad()
    #batch_size = 8

    losses = []
    #how often to print some info to stdout
    print_every = 25

    ind2w, w2ind, ind2c, c2ind, desc = dicts['ind2w'], dicts['w2ind'], dicts['ind2c'], dicts['c2ind'], dicts['desc']

    model.train()
    gen = DataLoader(dataset, batch_size=batch_size, shuffle=shuffle, num_workers=num_workers, collate_fn=collate, pin_memory=True)
        
    desc_data = desc
    if embed_desc and gpu:
        desc_data = desc_data.cuda()
    
    for batch_idx, tup in tqdm(enumerate(gen)):

        data, target, target_coarse, _, _ = tup
        target_cat = torch.cat([target_coarse, target], dim=1)

        if gpu:
            data, target, target_coarse = data.cuda(), target.cuda(), target_coarse.cuda()
            #target_cat = target_cat.cuda()
        
        batch_size, seq_length = data.size()[0:2]
        
        optimizer.zero_grad()

        if model.hier:
            _, loss, _ = model(data, target, target_coarse, desc_data=desc_data)

        else:
            _, loss, _ = model(data, target, desc_data=desc_data)
        
        del data, target, target_coarse
        #loss = loss / accumulation_steps 
        loss.backward()
        losses.append(loss.item())
        del loss
        
        #if (batch_idx+1) % accumulation_steps == 0 or batch_size < 16:
        optimizer.step()
        #    optimizer.zero_grad()
        
        if not quiet and batch_idx % print_every == 0:
            #print the average loss of the last 10 batches
            print("Train epoch: {} [batch #{}, batch_size {}, seq length {}]\tLoss: {:.6f}".format(
                epoch, batch_idx, batch_size, seq_length, np.mean(losses[-10:])))
    return losses

def test(model, Y, epoch, dataset, batch_size, embed_desc, fold, gpu, version, dicts, samples, model_dir):
    """
        Testing loop.
        Returns metrics
    """

    print('file for evaluation: %s' % fold)

    docs, attention, y, yhat, yhat_raw, hids, losses = [], [], [], [], [], [], []

    y_coarse, yhat_coarse, yhat_coarse_raw = [], [], []

    ind2w, w2ind, ind2c, c2ind, desc = dicts['ind2w'], dicts['w2ind'], dicts['ind2c'], dicts['c2ind'], dicts['desc']

    model.eval()

    gen = DataLoader(dataset, batch_size=batch_size, shuffle=False, num_workers=num_workers, collate_fn=collate)

    desc_data = desc
    if desc_data is not None and gpu:
        desc_data = desc_data.cuda()

    for batch_idx, tup in tqdm(enumerate(gen)):
        data, target, target_coarse, hadm_ids, data_text = tup
        target_cat = torch.cat([target_coarse, target], dim=1)
        
        if gpu:
            data, target, target_coarse = data.cuda(), target.cuda(), target_coarse.cuda()
            #target_cat = target_cat.cuda()

        model.zero_grad()

        if model.hier:
            output, loss, alpha = model(data, target, target_coarse, desc_data=desc_data)
        else:
            output, loss, alpha = model(data, target, desc_data=desc_data)

        if model.hier:
            output, output_coarse = output
            output_coarse = output_coarse.data.cpu().numpy()
            alpha, alpha_coarse = alpha
        else:
            output_coarse = np.zeros([len(output), len(dicts['ind2c_coarse'])])
            for i, y_hat_raw_ in enumerate(output.data.cpu().numpy()):
                if len(np.nonzero(np.round(y_hat_raw_))) == 0:
                    continue
                codes = [str(dicts['ind2c'][ind]) for ind in np.nonzero(np.round(y_hat_raw_))[0]]
                codes_coarse = set(str(code).split('.')[0] for code in codes)
                codes_coarse_idx = [dicts['c2ind_coarse'][code_coarse] for code_coarse in codes_coarse]
                output_coarse[i, codes_coarse_idx] = 1

        target_coarse_data = target_coarse.data.cpu().numpy()
        y_coarse.append(target_coarse_data)
        yhat_coarse_raw.append(output_coarse)
        yhat_coarse.append(np.round(output_coarse))
        
        losses.append(loss.item())
        target_data = target.data.cpu().numpy()
 
        del data, loss
        
        if fold == 'test':
            #alpha, _ = torch.max(torch.round(output).unsqueeze(-1).expand_as(alpha) * alpha, 1)
            #alpha = (torch.round(output).byte() | target.byte()).unsqueeze(-1).expand_as(alpha).type('torch.cuda.FloatTensor') * alpha
            alpha = [a for a in [a_m for a_m in alpha.data.cpu().numpy()]]
        else:
            alpha = []
        
        del target
        
        output = output.data.cpu().numpy()
        
        #save predictions, target, hadm ids
        yhat_raw.append(output)
        yhat.append(np.round(output))
        y.append(target_data)
        
        hids.extend(hadm_ids)
        docs.extend(data_text)
        #attention.extend(alpha)
        attention.extend(alpha)
        
    level = ''
    k = 5 if len(ind2c) == 50 else [8,15]

    y_coarse = np.concatenate(y_coarse, axis=0)
    yhat_coarse = np.concatenate(yhat_coarse, axis=0)
    yhat_coarse_raw = np.concatenate(yhat_coarse_raw, axis=0)
    metrics_coarse, _, _ = evaluation.all_metrics(yhat_coarse, y_coarse, k=k, yhat_raw=yhat_coarse_raw, level='coarse')
    evaluation.print_metrics(metrics_coarse, level='coarse')

    y = np.concatenate(y, axis=0)
    yhat = np.concatenate(yhat, axis=0)
    yhat_raw = np.concatenate(yhat_raw, axis=0)
    
    #get metrics
    metrics, metrics_codes, metrics_inst = evaluation.all_metrics(yhat, y, k=k, yhat_raw=yhat_raw, level='fine')
    evaluation.print_metrics(metrics, level='fine')
    metrics['loss'] = np.mean(losses)
    metrics.update(metrics_coarse)

    #write the predictions
    if fold == 'test':
        persistence.write_preds(hids, docs, attention, y, yhat, yhat_raw, metrics_inst, model_dir, fold, ind2c, c2ind, dicts['desc_plain'])
    
    return metrics, metrics_codes, metrics_inst, hids

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="train a neural network on some clinical documents")
    parser.add_argument("data_path", type=str,
                        help="path to a file containing sorted train data. dev/test splits assumed to have same name format with 'train' replaced by 'dev' and 'test'")
    parser.add_argument("vocab", type=str, help="path to a file holding vocab word list for discretizing words")
    parser.add_argument("Y", type=str, help="size of label space")
    parser.add_argument("model", type=str, choices=["cnn_vanilla", "rnn", "conv_attn", "conv_attn_old", "multi_conv_attn", "hier_conv_attn", "saved", "dummy"], help="model")
    parser.add_argument("n_epochs", type=int, help="number of epochs to train")
    parser.add_argument("--embed-file", type=str, required=False, dest="embed_file",
                        help="path to a file holding pre-trained embeddings")
    parser.add_argument("--cell-type", type=str, choices=["lstm", "gru"], help="what kind of RNN to use (default: GRU)", dest='cell_type',
                        default='gru')
    parser.add_argument("--rnn-dim", type=int, required=False, dest="rnn_dim", default=128,
                        help="size of rnn hidden layer (default: 128)")
    parser.add_argument("--bidirectional", dest="bidirectional", action="store_const", required=False, const=True,
                        help="optional flag for rnn to use a bidirectional model")
    parser.add_argument("--rnn-layers", type=int, required=False, dest="rnn_layers", default=1,
                        help="number of layers for RNN models (default: 1)")
    parser.add_argument("--embed-size", type=int, required=False, dest="embed_size", default=100,
                        help="size of embedding dimension. (default: 100)")
    parser.add_argument("--embed-trainable", action='store_true', dest="embed_trainable",
                        help="optional flag to make word embeddings trainable")
    parser.add_argument("--embed-normalize", action='store_true', dest="embed_normalize",
                        help="optional flag to normalize word embeddings")
    parser.add_argument("--shuffle", action='store_true', dest="shuffle",
                        help="optional flag to shuffle training dataset at each epoch")                    
    parser.add_argument("--filter-size", type=str, required=False, dest="filter_size", default=4,
                        help="size of convolution filter to use. (default: 4) For multi_conv_attn, give comma separated integers, e.g. 3,4,5")
    parser.add_argument("--filter-size-words", type=int, required=False, dest="filter_size_words", default=5,
                        help="size of words convolution filter to use. (default: 5)")
    parser.add_argument("--filter-size-sents", type=int, required=False, dest="filter_size_sents", default=3,
                        help="size of words convolution filter to use. (default: 3)")
    parser.add_argument("--num-filter-maps", type=int, required=False, dest="num_filter_maps", default=50,
                        help="size of conv output (default: 50)")
    parser.add_argument("--num-filter-maps-words", type=int, required=False, dest="num_filter_maps_words", default=50,
                        help="size of words conv output (default: 50)")
    parser.add_argument("--num-filter-maps-sents", type=int, required=False, dest="num_filter_maps_sents", default=25,
                        help="size of sents conv output (default: 25)")
    parser.add_argument("--weight-decay", type=float, required=False, dest="weight_decay", default=0,
                        help="coefficient for penalizing l2 norm of model weights (default: 0)")
    parser.add_argument("--lr", type=float, required=False, dest="lr", default=1e-3,
                        help="learning rate for Adam optimizer (default=1e-3)")
    parser.add_argument("--batch-size", type=int, required=False, dest="batch_size", default=16,
                        help="size of training batches")
    parser.add_argument("--dropout", dest="dropout", type=float, required=False, default=0.5,
                        help="optional specification of dropout (default: 0.5)")
    parser.add_argument("--dropout-sents", dest="dropout_sents", type=float, required=False, default=0.5,
                        help="optional specification of dropout for sentence layer (default: 0.5)")
    parser.add_argument("--dataset", type=str, choices=['mimic2', 'mimic3'], dest="version", default='mimic3', required=False,
                        help="version of MIMIC in use (default: mimic3)")
    parser.add_argument("--test-model", type=str, dest="test_model", required=False, help="path to a saved model to load and evaluate")
    parser.add_argument("--criterion", type=str, default='f1_micro', required=False, dest="criterion",
                        help="which metric to use for early stopping (default: f1_micro)")
    parser.add_argument("--patience", type=int, default=3, required=False,
                        help="how many epochs to wait for improved criterion metric before early stopping (default: 3)")
    parser.add_argument("--gpu", dest="gpu", action="store_const", required=False, const=True,
                        help="optional flag to use GPU if available")
    parser.add_argument("--public-model", dest="public_model", action="store_const", required=False, const=True,
                        help="optional flag for testing pre-trained models from the public github")
    parser.add_argument("--stack-filters", dest="stack_filters", action="store_const", required=False, const=True,
                        help="optional flag for multi_conv_attn to instead use concatenated filter outputs, rather than pooling over them")
    parser.add_argument("--samples", dest="samples", action="store_const", required=False, const=True,
                        help="optional flag to save samples of good / bad predictions")
    parser.add_argument("--quiet", dest="quiet", action="store_const", required=False, const=True,
                        help="optional flag not to print so much during training")
    parser.add_argument("--max-len", type=int, required=False, dest="max_len", default=-1,
                        help="set maximum number of tokens per document (optional)")
    parser.add_argument("--hier", action="store_true", dest="hier",
                        help="hierarchical predictions (defaul false)")
    parser.add_argument("--embed-desc", action="store_true", dest="embed_desc")
    parser.add_argument("--exclude-non-billable", action="store_true", dest="exclude_non_billable")
    parser.add_argument("--include-invalid", action="store_true", dest="include_invalid")
    parser.add_argument("--layer-norm", action="store_true", dest="layer_norm")
    args = parser.parse_args()
    command = ' '.join(['python'] + sys.argv)
    args.command = command
    main(args)

