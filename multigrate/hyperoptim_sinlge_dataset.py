import argparse
import numpy as np
import time
import os
import json
from itertools import cycle
import scanpy as sc
from .utils import parse_config_file, split_adatas
from .datasets import load_dataset
from .models import create_model
import torch
from hyperopt import fmin, tpe, hp, STATUS_OK, Trials
from .train import train
from .validate import validate
from functools import partial
from copy import deepcopy
import warnings
warnings.simplefilter(action='ignore', category=FutureWarning)


def objective(params, base_experiment_name, output_dir, pair_split, save_losses, config):
    experiment_name = f'{base_experiment_name}_kl({params["kl_coef"]:.7f})_cycle({params["cycle_coef"]:.7f})_pair({pair_split:.2f})'
    output_dir = os.path.join(output_dir, experiment_name)

    config = deepcopy(config)
    model_params = config['model']['params']
    for p in ['kl_coef', 'cycle_coef']:
        model_params[p] = params[p]
    train(experiment_name, output_dir, **config)

    config = parse_config_file(os.path.join(output_dir, 'config.json'))
    validate(base_experiment_name, output_dir, config)

    metrics = parse_config_file(os.path.join(output_dir, 'metrics.json'))
    mean_integ_metrics = np.mean([metrics[i] for i in ['graph_conn']]) # 'ASW_label/batch', 'PCR_batch',
    mean_bio_metrics = np.mean([metrics[i] for i in ['ASW_label', 'NMI_cluster/label', 'ARI_cluster/label', 'isolated_label_silhouette']]) # , 'isolated_label_F1'

    return {
        'loss': -(mean_integ_metrics + mean_bio_metrics),
        'status': STATUS_OK,
        'eval_time': time.time()
    }


def hyper_optimize(base_experiment_name, output_dir, config, max_evals, kl_coefs_range, cycle_coefs_range, pair_split, save_losses):
    # define the search space
    space = hp.choice('model_params', [{
        'kl_coef': hp.loguniform('kl_coef', *kl_coefs_range),
        'cycle_coef': hp.loguniform('cycle_coef', *cycle_coefs_range)
    }])

    trials = Trials()
    fmin_objective = partial(objective, base_experiment_name=base_experiment_name, output_dir=output_dir, pair_split=pair_split, config=config, save_losses=save_losses)
    best = fmin(fmin_objective, space=space, algo=tpe.suggest, max_evals=max_evals, trials=trials)
    return best


def parse_args():
    parser = argparse.ArgumentParser(description='Perform hyper-parameter optimization.')
    parser.add_argument('--base-config-file', type=str, required=True)
    parser.add_argument('--output-dir', type=str, required=True)
    parser.add_argument('--kl-coefs-range', nargs=2, type=float, required=True)
    parser.add_argument('--cycle-coefs-range', nargs=2, type=float, required=True)
    parser.add_argument('--pair-split', type=float, required=True)
    parser.add_argument('--max-evals', type=int, default=100)
    parser.add_argument('--save-losses', type=bool, default=True)
    return parser.parse_args()


if __name__ == '__main__':
    args = parse_args()
    config = parse_config_file(args.base_config_file)
    base_experiment_name = os.path.splitext(os.path.basename(args.base_config_file))[0]

    best = hyper_optimize(
        base_experiment_name,
        args.output_dir,
        config,
        args.max_evals,
        args.kl_coefs_range,
        args.cycle_coefs_range,
        args.pair_split,
        args.save_losses
    )
    print(best)