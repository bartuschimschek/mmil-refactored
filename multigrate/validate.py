import argparse
import numpy as np
import pandas as pd
import time
import os
import json
from collections import OrderedDict
import matplotlib.pyplot as plt
import scanpy as sc
from .utils import parse_config_file, split_adatas
from .metrics import metrics
from .datasets import load_dataset
from .models import create_model
import torch

def validate(experiment_name, output_dir, config, save_losses=True):
    # load experiment configurations
    experiment_config = config['experiment']
    random_seed = experiment_config['seed']
    pair_split = experiment_config.get('pair-split', None)
    batches_present = experiment_config['batch']

    train_params = config['model']['train']
    modality_key = train_params.get('modality_key', 'modality')
    celltype_key = train_params.get('celltype_key', 'cell_type')
    # torch.manual_seed(random_seed)

    # load adatas
    model_params = config['model']['params']
    adatas = []
    for adata_set in model_params['adatas']:
        adatas.append([])
        for adata_path in adata_set:
            adata = sc.read_h5ad(adata_path)
            adatas[-1].append(adata)
    model_params['adatas'] = adatas

    # recover the paired/unpaired splitting done by the training script
    if pair_split is not None:
        pair_group_masks = torch.load(os.path.join(output_dir, 'pair-group-masks.pt'))
        adatas, names, pair_groups, _ = split_adatas(adatas, model_params['names'], model_params['pair_groups'], pair_split, pair_group_masks, shuffle_unpaired=False)
        model_params['adatas'] = adatas
        model_params['names'] = names
        model_params['pair_groups'] = pair_groups

    # load the model
    model = create_model(config['model']['name'], model_params)
    model.load(output_dir)
    if save_losses:
        save_losses_figure(model, output_dir)

    # validate the model
    with torch.no_grad():
        # predict the shared latent space
        z = model.predict(
            adatas,
            names,
            pair_groups=pair_groups,
            batch_size=train_params['batch_size'],
            modality_key=modality_key,
            celltype_key=celltype_key
        )

        # plot the unintegrated latents
        sc.pp.neighbors(z)
        sc.tl.umap(z)
        sc.pl.umap(z, color=[modality_key, celltype_key], ncols=1, show=False)
        plt.savefig(os.path.join(output_dir, 'umap-z.png'), dpi=200, bbox_inches='tight')
        plt.close('all')

        # calculate metrics and save them
        # sc.pp.pca(z)
        z.obsm['X_latent'] = z.X
        mtrcs = metrics(
            z, z,
            batch_key=modality_key,
            label_key=celltype_key,
            embed='X_latent',
            pcr_batch=False,
            isolated_label_f1=False,
            asw_batch=batches_present
        )
        print(mtrcs.to_dict())
        json.dump(mtrcs.to_dict()['score'], open(os.path.join(output_dir, 'metrics.json'), 'w'), indent=2)


def parse_args():
    parser = argparse.ArgumentParser(description='Perform model validation.')
    parser.add_argument('--root-dir', type=str)
    return parser.parse_args()

def save_losses_figure(model, output_dir):
    plt.figure(figsize=(15, 10));

    loss_names = ['recon', 'kl', 'integ', 'cycle']
    nrows = int(np.ceil((len(loss_names)+1)/2))

    plt.subplot(nrows, 2, 1)
    plt.plot(model.history['iteration'], model.history['train_loss'], '.-', label='Train loss');
    plt.plot(model.history['iteration'], model.history['val_loss'], '.-', label='Val loss');
    plt.legend()

    for i, name in enumerate(loss_names):
        plt.subplot(nrows, 2, i+2)
        plt.plot(model.history['iteration'], model.history[f'train_{name}'], '.-', label=f'Train {name} loss');
        plt.plot(model.history['iteration'], model.history[f'val_{name}'], '.-', label=f'Val {name} loss');
        plt.legend()

    plt.savefig(os.path.join(output_dir, f'losses.png'), dpi=200, bbox_inches='tight')
    plt.close('all')

if __name__ == '__main__':
    args = parse_args()
    config = parse_config_file(os.path.join(args.root_dir, 'config.json'))
    experiment_name = os.path.basename(args.root_dir)

    validate(experiment_name, args.root_dir, config)
