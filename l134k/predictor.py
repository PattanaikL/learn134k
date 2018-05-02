#!/usr/bin/env python
# -*- coding:utf-8 -*-

import logging
import os
import shutil

import numpy as np

from rmgpy.cnn_framework.cnn_model import build_model, train_model, save_model, reset_model
from rmgpy.cnn_framework.molecule_tensor import get_attribute_vector_size
from rmgpy.cnn_framework.data import (split_test_from_train_and_val, split_inner_val_from_train_data,
                                      prepare_folded_data, prepare_data_one_fold)

from util import pickle_dump, pickle_load, calculate_rmse, calculate_mae


class Predictor(object):
    def __init__(self, out_dir=None):
        self.model = None
        self.out_dir = out_dir
        self.y_mean = None
        self.y_std = None

    def build_model(self, tensor_settings, **model_settings):
        attribute_vector_size = get_attribute_vector_size(
            add_extra_atom_attribute=tensor_settings['add_extra_atom_attribute'],
            add_extra_bond_attribute=tensor_settings['add_extra_bond_attribute']
        )
        self.model = build_model(attribute_vector_size=attribute_vector_size, **model_settings)

    def reset_model(self):
        self.model = reset_model(self.model)

    def load_weights(self, model_weights_path):
        logging.info('Loading model weights from {}'.format(model_weights_path))
        self.model.load_weights(model_weights_path)

    def load_mean_and_std(self, mean_and_std_path):
        self.y_mean, self.y_std = pickle_load(mean_and_std_path)

    def normalize(self, y_train, *other_ys):
        self.y_mean = np.mean(y_train, axis=0)
        self.y_std = np.std(y_train, axis=0)
        logging.info('Mean: {} kcal/mol, std: {} kcal/mol'.format(self.y_mean, self.y_std))

        y_train = (y_train - self.y_mean) / self.y_std
        other_ys = tuple((y-self.y_mean)/self.y_std for y in other_ys)
        return y_train, other_ys

    def split_test(self, x, y, names, test_split, save_names=False):
        logging.info('Splitting dataset with a test split of {}'.format(test_split))
        x_test, y_test, x_train, y_train, names_test, names_train = split_test_from_train_and_val(
            x, y, extra_data=names, shuffle_seed=7, testing_ratio=test_split
        )

        if save_names:
            names_test_path = os.path.join(self.out_dir, 'names_test.txt')
            names_train_path = os.path.join(self.out_dir, 'names_train.txt')
            with open(names_test_path, 'w') as f:
                for name in names_test:
                    f.write(name + '\n')
            with open(names_train_path, 'w') as f:
                for name in names_train:
                    f.write(name + '\n')

        return x_test, y_test, x_train, y_train

    def kfcv_train(self, x, y, names, folds, test_split, train_ratio,
                   test_data=None, save_names=False, pretrained_weights=None, **train_settings):
        if test_data is not None:
            test_split = 0
        x_test, y_test, x_train_and_val, y_train_and_val = self.split_test(
            x, y, names, test_split, save_names=save_names
        )
        if test_data is not None:
            x_test, y_test = test_data
        folded_xs, folded_ys = prepare_folded_data(x_train_and_val, y_train_and_val, folds, shuffle_seed=2)

        losses = []
        inner_val_losses = []
        outer_val_losses = []
        test_losses = []
        for fold in range(folds):
            x_train, x_inner_val, x_outer_val, y_train, y_inner_val, y_outer_val = prepare_data_one_fold(
                folded_xs, folded_ys, current_fold=fold, shuffle_seed=4, training_ratio=train_ratio
            )
            y_train, (y_inner_val, y_outer_val, y_test) = self.normalize(y_train, y_inner_val, y_outer_val, y_test)

            self.model, loss, inner_val_loss, mean_outer_val_loss, mean_test_loss = train_model(
                self.model, x_train, y_train, x_inner_val, y_inner_val, x_test, y_test,
                X_outer_val=x_outer_val, y_outer_val=y_outer_val, **train_settings
            )

            losses.append(loss)
            inner_val_losses.append(inner_val_loss)
            outer_val_losses.append(mean_outer_val_loss)
            test_losses.append(mean_test_loss)

            rmse_train, mae_train = self.evaluate(x_train, y_train, norm=True)
            if x_inner_val.size:
                rmse_inner_val, mae_inner_val = self.evaluate(x_inner_val, y_inner_val, norm=True)
            else:
                rmse_inner_val, mae_inner_val = np.NaN, np.NaN
            rmse_outer_val, mae_outer_val = self.evaluate(x_outer_val, y_outer_val, norm=True)
            if x_test.size:
                rmse_test, mae_test = self.evaluate(x_test, y_test, norm=True)
            else:
                rmse_test, mae_test = np.NaN, np.NaN
            logging.info('Final statistics:')
            logging.info(
                '\t\t\tRMSE\tMAE\n'
                'Train\t\t{:.2f}\t{:.2f}\n'
                'Inner val\t{:.2f}\t{:.2f}\n'
                'Outer val\t{:.2f}\t{:.2f}\n'
                'Test\t\t{:.2f}\t{:.2f}'.format(
                    rmse_train, mae_train,
                    rmse_inner_val, mae_inner_val,
                    rmse_outer_val, mae_outer_val,
                    rmse_test, mae_test
                )
            )

            model_path = os.path.join(self.out_dir, 'model_fold_{}'.format(fold))
            self.save_model(model_path, loss, inner_val_loss, mean_outer_val_loss, mean_test_loss)

            # once finish training one fold, reset the model
            if pretrained_weights is not None:
                self.load_weights(pretrained_weights)
            else:
                self.reset_model()

    def full_train(self, x, y, names, test_split, train_ratio, test_data=None, save_names=False, **train_settings):
        if test_data is not None:
            test_split = 0.0
        x_test, y_test, x_train, y_train = self.split_test(x, y, names, test_split, save_names=save_names)
        if test_data is not None:
            x_test, y_test = test_data

        logging.info('Splitting training data into early-stopping validation and remaining training sets')
        x_train, x_inner_val, y_train, y_inner_val = split_inner_val_from_train_data(
            x_train, y_train, shuffle_seed=77, training_ratio=train_ratio
        )
        y_train, (y_inner_val, y_test) = self.normalize(y_train, y_inner_val, y_test)

        logging.info('Training model...')
        logging.info('Training data: {} points'.format(len(x_train)))
        logging.info('Inner validation data: {} points'.format(len(x_inner_val)))
        logging.info('Test data: {} points'.format(len(x_test)))
        self.model, loss, inner_val_loss, mean_outer_val_loss, mean_test_loss = train_model(
            self.model, x_train, y_train, x_inner_val, y_inner_val, x_test, y_test,
            X_outer_val=None, y_outer_val=None, **train_settings
        )

        rmse_train, mae_train = self.evaluate(x_train, y_train, norm=True)
        if x_inner_val.size:
            rmse_inner_val, mae_inner_val = self.evaluate(x_inner_val, y_inner_val, norm=True)
        else:
            rmse_inner_val, mae_inner_val = np.NaN, np.NaN
        if x_test.size:
            rmse_test, mae_test = self.evaluate(x_test, y_test, norm=True)
        else:
            rmse_test, mae_test = np.NaN, np.NaN
        logging.info('Final statistics (kcal/mol):')
        logging.info(
            '\t\t\tRMSE\tMAE\n'
            'Train\t\t{:.2f}\t{:.2f}\n'
            'Inner val\t{:.2f}\t{:.2f}\n'
            'Test\t\t{:.2f}\t{:.2f}'.format(
                rmse_train, mae_train, rmse_inner_val, mae_inner_val, rmse_test, mae_test
            )
        )

        model_path = os.path.join(self.out_dir, 'model')
        self.save_model(model_path, loss, inner_val_loss, mean_outer_val_loss, mean_test_loss)

    def save_model(self, model_path, loss, inner_val_loss, mean_outer_val_loss, mean_test_loss):
        logging.info('Saving model')
        model_structure_path = model_path + '.json'
        model_weights_path = model_path + '.h5'
        mean_and_std_path = model_path + '.attr'
        if os.path.exists(model_structure_path):
            logging.info(
                'Backing up model structure (and removing old backup if present): {}'.format(model_structure_path))
            shutil.move(model_structure_path, model_path + '_backup.json')
        if os.path.exists(model_weights_path):
            logging.info('Backing up model weights (and removing old backup if present): {}'.format(model_weights_path))
            shutil.move(model_weights_path, model_path + '_backup.h5')
        if os.path.exists(mean_and_std_path):
            logging.info('Backing up mean and std (and removing old backup if present): {}'.format(mean_and_std_path))
            shutil.move(mean_and_std_path, model_path + '_backup.attr')
        save_model(self.model, loss, inner_val_loss, mean_outer_val_loss, mean_test_loss, model_path)
        pickle_dump(mean_and_std_path, (self.y_mean, self.y_std))

    def predict(self, x, norm=False):
        y_norm = self.model.predict(x).flatten()
        if norm:
            return y_norm
        else:
            if self.y_mean is None or self.y_std is None:
                raise Exception('Missing mean and/or std of training data')
            else:
                return y_norm * self.y_std + self.y_mean

    def evaluate(self, x, y, norm=False):
        y_pred = self.predict(x, norm=norm).flatten()
        rmse = calculate_rmse(y, y_pred)
        mae = calculate_mae(y, y_pred)
        if norm:
            return self.y_std**2.0 * rmse, self.y_std**2.0 * mae
        else:
            return rmse, mae
