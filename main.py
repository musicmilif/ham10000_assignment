import os
import sys
import argparse
import logging
import numpy as np
from sklearn.metrics import confusion_matrix
import torch
from torch import nn
from torch import optim
from torch.utils.data import DataLoader
from torch.utils.data.sampler import RandomSampler, WeightedRandomSampler
from pretrainedmodels import model_names

import matplotlib
matplotlib.use('agg')

from src.utils import (generate_meta, 
                       stratified_split, 
                       over_sample,
                       create_loss_plot, 
                       setup_logging, 
                       create_confusion_matrix)
from src.data_utils import HAMDataset, build_train_transform, build_test_transform, build_preprocess
from modeling.model import HAMNet
from modeling.utils import AverageMeter, save_checkpoint, load_checkpoint

STATUS_MSG_T = "Batches done: {}/{} | Loss: {:6f} | Accuracy: {:6f} | AvgPrecision: {:.6f}"
STATUS_MSG_V = "Epochs done: {}/{} | Loss: {:6f} | Accuracy: {:6f} | AvgPrecision: {:.6f}"
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')


def main(args):
    # Logging
    LOGGER = logging.getLogger(__name__)
    exp_dir = os.path.join('experiments', f'{args.backbone}_v{args.version}')
    log_file = os.path.join(exp_dir, 'log.log')
    loss_file = os.path.join(exp_dir, 'results_loss.npy')
    confusion_file = os.path.join(exp_dir, 'results_confusion.npy')
    os.makedirs(exp_dir, exist_ok=True)
    setup_logging(log_path=log_file, log_level=args.log_level, logger=LOGGER)
    args_file = os.path.join(exp_dir, 'args.log')
    with open(args_file, 'w') as f:
        f.write(str(args))

    # Initialize datasets and loaders.
    LOGGER.info('Data Processing...')

    df = generate_meta(args.data_dir)
    train_ids, valid_ids = stratified_split(df)
    n_classes = df['target'].max()+1

    model = HAMNet(n_classes, model_name=args.backbone)
    model = model.to(device)

    train_df = df.loc[df['image_id'].isin(train_ids)]
    train_df = over_sample(train_df)
    valid_df = df.loc[df['image_id'].isin(valid_ids)]

    train_dataset = HAMDataset(
        train_df,
        build_preprocess(model.mean, model.std),
        build_train_transform()
        )
    valid_dataset = HAMDataset(
        valid_df,
        build_preprocess(model.mean, model.std),
        build_test_transform()
        )

    train_loader = DataLoader(
        train_dataset,
        batch_size=args.batch_size,
        shuffle=True,
        num_workers=10
        )
    valid_loader = DataLoader(
        valid_dataset, 
        batch_size=args.batch_size,
        shuffle=False,
        num_workers=10
        )

    optimizer = optim.Adam(model.parameters(), lr=1e-3)
    criterion = nn.CrossEntropyLoss().to(device)

    start_epoch = 1
    if args.load_weight:
        ckpt = utils.load_checkpoint(path=args.load_weight, model=model, optimizer=optimizer, epoch=True)
        model, optimizer, start_epoch = ckpt['model'], ckpt['optimizer'], ckpt['epoch'] + 1
        model = model.to(device)

    best_acc = 0
    epochs, train_losses, valid_losses = [], [], []
    for epoch in range(start_epoch, start_epoch+args.num_epochs):
        # Training
        LOGGER.info(f'Epoch: {epoch}')
        model.train()
        n_batches = len(train_loader.dataset) // args.batch_size + 1

        train_loss = AverageMeter()
        train_acc = AverageMeter()
        train_map = AverageMeter()

        for batch_idx, (inputs, targets, _) in enumerate(train_loader):
            inputs, targets = inputs.to(device), targets.type(torch.LongTensor).to(device)
            outputs = model(inputs)
            loss = criterion(outputs, targets)

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            _, predicted = outputs.max(dim=1)

            # Update Metrics
            from src.metrics import avg_precision
            train_loss.update(loss.item())
            train_acc.update(predicted.eq(targets).sum().item()/targets.size(0))
            train_map.update(avg_precision(targets.cpu().detach().numpy(), 
                                           predicted.cpu().detach().numpy()))

            if batch_idx % 10 == 9:
                LOGGER.info(STATUS_MSG_T.format(batch_idx+1,
                                                n_batches,
                                                train_loss.avg,
                                                train_acc.avg,
                                                train_map.avg))
        # Validation
        val_ids, val_labels, val_preds = [], [], []
        model.eval()

        valid_loss = AverageMeter()
        valid_acc = AverageMeter()
        valid_map = AverageMeter()

        with torch.no_grad():
            for batch_idx, (inputs, targets, img_id) in enumerate(valid_loader):
                inputs, targets = inputs.to(device), targets.type(torch.LongTensor).to(device)
                outputs = model(inputs)
                loss = criterion(outputs, targets)

                _, predicted = outputs.max(dim=1)
                valid_loss.update(loss.item())
                valid_acc.update(predicted.eq(targets).sum().item()/targets.size(0))
                valid_map.update(avg_precision(targets.cpu().numpy(), 
                                               outputs.cpu().numpy()))

                val_ids.extend(img_id)
                val_labels.extend(targets.cpu().numpy())
                val_preds.extend(np.squeeze(predicted.cpu().numpy().T))

        LOGGER.info(STATUS_MSG_V.format(epoch,
                                        args.num_epochs,
                                        valid_loss.avg,
                                        valid_acc.avg,
                                        valid_map.avg))

        # Save checkpoint.
        if valid_acc.avg > best_acc:
            LOGGER.info('Saving..')
            output_file_name = os.path.join(exp_dir, f'checkpoint_{valid_acc.avg:.3f}.ckpt')
            save_checkpoint(path=output_file_name,
                            model=model,
                            epoch=epoch,
                            optimizer=optimizer)
            best_acc = valid_acc.avg

        epochs.append(epoch)
        train_losses.append(train_loss.avg)
        valid_losses.append(valid_loss.avg)
        create_loss_plot(exp_dir, epochs, train_losses, valid_losses)
        np.save(loss_file, [train_losses, valid_losses])
        np.save(confusion_file, [val_ids, val_labels, val_preds])
        confusion_mtx = confusion_matrix(val_labels, val_preds)
        plot_labels = ['akiec', 'bcc', 'bkl', 'df', 'nv', 'vasc','mel']
        create_confusion_matrix(exp_dir, confusion_mtx, plot_labels, normalize=True)


def parse_arguments(argv):
    parser = argparse.ArgumentParser(description='PyTorch classifier on HAM10000 dataset')
    parser.add_argument('--data-dir', default='/disk/HAM10000/', help='path to data')
    parser.add_argument('--version', default=1, type=int,
                        help='version of experiment')
    parser.add_argument('--log-level', default='INFO', choices = ['DEBUG', 'INFO'],
                        help='log-level to use')
    parser.add_argument('--batch-size', default=64, type=int,
                        help='batch-size to use')
    parser.add_argument('--backbone', default='resnet18', choices=model_names,
                        help='network architecture')
    parser.add_argument('--num-epochs', default=10, type=int,
                        help='Number of training epochs')
    parser.add_argument('--load-weight', default='', type=str,
                    help='Load pre-trained weight')

    return parser.parse_args(argv)


if __name__ == '__main__':
    main(parse_arguments(sys.argv[1:]))