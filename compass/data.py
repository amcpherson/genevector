import numpy as np
import torch
from torch.utils.data import Dataset
from multiprocessing import Pool
import scanpy as sc
import numpy
from scipy.sparse import csr_matrix, find
import operator
import itertools
from itertools import permutations
import argparse
import tqdm
import random
import pickle
import collections
import sys
import os
from sklearn.preprocessing import MinMaxScaler
from sklearn.neighbors import KernelDensity
import matplotlib.pyplot as plt
from collections import Counter
from scipy import stats
import itertools
from sklearn.linear_model import LinearRegression


class Context(object):

    def __init__(self):
        pass

    @classmethod
    def build(context_class, adata, subsample=None, expression=None, frequency_lower_bound = 10, threads=2):
        try:
            adata.var.index = [x.decode("utf-8") for x in adata.var.index]
        except Exception as e:
            pass
        context = context_class()
        if subsample:
            sc.pp.subsample(adata,fraction=subsample)
        context.adata = adata
        context.threads = threads
        context.genes = [x.upper() for x in list(context.adata.var.index)]
        context.normalized_matrix = context.adata.X
        context.metadata = context.adata.obs
        context.frequency_lower_bound = frequency_lower_bound
        try:
            for column in context.metadata.columns:
                if type(context.metadata[column][0]) == bytes:
                    context.metadata[column] = [x.decode("utf-8") for x in context.metadata[column]]
        except Exception as e:
            pass
        context.cells = context.adata.obs.index
        context.cell_index, context.index_cell = Context.index_cells(context.cells)
        context.data, context.cell_to_gene = context.expression(context.normalized_matrix, \
                            context.genes, \
                            context.index_cell,
                            expression=expression)
        context.expressed_genes = context.get_expressed_genes(context.data)
        context.gene_index, context.index_gene = Context.index_geneset(context.expressed_genes)
        context.gene2id = context.gene_index
        context.id2gene = context.index_gene
        context.gene_count = len(context.gene_frequency.keys())
        context.adata = adata
        return context

    @classmethod
    def load(context_class, path):
        context = context_class()
        serialized = pickle.load(open(path, "rb"))
        context.unserialize(serialized)
        context.path = os.path.split(path)[0]
        return context

    @staticmethod
    def index_geneset(genes):
        gene_index = {w: idx for (idx, w) in enumerate(genes)}
        index_gene = {idx: w for (idx, w) in enumerate(genes)}
        return gene_index, index_gene

    @staticmethod
    def index_cells(cells):
        cell_index = {w: idx for (idx, w) in enumerate(cells)}
        index_cell = {idx: w for (idx, w) in enumerate(cells)}
        return cell_index, index_cell

    def get_expressed_genes(self, data):
        return list(data.keys())

    def get_expressed_genes_frequency(self, data):
        return self.gene_frequency

    def inverse_filter(self, data):
        cell_to_gene = collections.defaultdict(list)
        for gene, cells in data.items():
            for cell in cells:
                cell_to_gene[cell].append(gene)
        return cell_to_gene

    def expression(self, normalized_matrix, genes, cells, expression=None):
        gene_index, index_gene = Context.index_geneset(genes)
        self.gene_frequency = collections.defaultdict(int)
        data = collections.defaultdict(list)
        if expression == None:
            self.expression = collections.defaultdict(dict)
            nonzero = find(normalized_matrix)
            print("Loading Expression.")
            
            nonindexed_expression = collections.defaultdict(dict)
            for cell, gene_i, val in tqdm.tqdm(list(zip(*nonzero))):
                symbol = index_gene[gene_i]
                nonindexed_expression[cell][symbol] = val
            
            print("Reindexing Cooc")
            for cell, genes in tqdm.tqdm(list(nonindexed_expression.items())):
                barcode = cells[cell]
                for index, val in genes.items():
                    self.expression[barcode][index] = val
                    data[index].append(barcode)
                    self.gene_frequency[index] += 1
        else:
            self.expression = pickle.load(open(expression,"rb"))
            for cell, genes in tqdm.tqdm(list(self.expression.items())):
                for gene, val in genes.items():
                    data[gene].append(cell)
                    self.gene_frequency[gene] += 1

        data = self.filter_on_frequency(data)
        return data, self.inverse_filter(data)


    def filter_on_frequency(self, data):
        remove = []
        for gene, frequency in self.gene_frequency.items():
            if frequency < self.frequency_lower_bound:
                del data[gene]
                remove.append(gene)
        for gene in remove:
            del self.gene_frequency[gene]
        return data

    def serialize(self):
        serialized = dict()
        for attr, value in self.__dict__.items():
            if attr != "adata" and attr != "inv_data" and attr != "data":
                serialized[attr] = value
        return serialized

    def unserialize(self, serialized):
        for attribute, value in serialized.items():
            setattr(self, attribute, value)

    def save(self, filename):
        serialized = self.serialize()
        pickle.dump(serialized, open(filename,"wb"))

    def frequency_histogram(self):
        f = np.array(list(self.gene_frequency.values())) / len(self.cells)
        plt.hist(f, 200, density=True, facecolor='g', alpha=0.75)
        plt.grid(True)
        plt.show()

    def frequency(self, gene):
        return self.gene_frequency[gene] / len(self.cells)

class CompassDataset(Dataset):

    def __init__(self, adata, features=[], expression=None, coocc=None, device="cpu"):
        self.data = Context.build(adata,expression=expression)
        self._word2id = self.data.gene2id
        self._id2word = self.data.id2gene
        self._vocab_len = len(self._word2id)
        self.features = features
        self.device = device
        self.create_coocurrence_matrix(coocc=coocc)
        print("Vocabulary length: {}".format(self._vocab_len))
        

    def create_coocurrence_matrix(self, coocc=None):
        print("Generating Correlation matrix.")
        import pandas
        all_genes = self.data.expressed_genes
        if coocc == None:
            corr_df = pandas.DataFrame.from_dict(self.data.expression)
            corr_df = corr_df.fillna(0.0)
            corr_df[corr_df != 0] = 1.0
            corr_df = corr_df.T
            coocc = numpy.array(corr_df.T.dot(corr_df))
            cov = numpy.corrcoef(corr_df)
        else:
            coocc = pickle.load(open(coocc,"rb"))

        self._i_idx = list()
        self._j_idx = list()
        self._xij = list()

        for gene, row in zip(all_genes, cov):
            for cgene, value in zip(all_genes, row):
                wi = self.data.gene2id[gene]
                ci = self.data.gene2id[cgene]
                self._i_idx.append(wi)
                self._j_idx.append(ci)
                if value > 0.0:
                    self._xij.append(float(value) * coocc[wi,ci] + 1.0)
                else:
                    self._xij.append(1.0)

        if self.device == "cuda":
            self._i_idx = torch.cuda.LongTensor(self._i_idx).cuda()
            self._j_idx = torch.cuda.LongTensor(self._j_idx).cuda()
            self._xij = torch.cuda.FloatTensor(self._xij).cuda()
        else:
            self._i_idx = torch.LongTensor(self._i_idx).to("cpu")
            self._j_idx = torch.LongTensor(self._j_idx).to("cpu")
            self._xij = torch.FloatTensor(self._xij).to("cpu")

        self.coocc = coocc
        self.cov = cov


    def get_batches(self, batch_size):
        rand_ids = torch.cuda.LongTensor(np.random.choice(len(self._xij), len(self._xij), replace=False))
        for p in range(0, len(rand_ids), batch_size):
            batch_ids = rand_ids[p:p+batch_size]
            yield self._xij[batch_ids], self._i_idx[batch_ids], self._j_idx[batch_ids]
