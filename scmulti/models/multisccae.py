import torch
from torch import nn
from torch.nn import functional as F
from .mlp import MLP
from .losses import MMD


class MultiScCAE(nn.Module):
    def __init__(self, x_dims,
                 z_dim=10,
                 h_dim=32,
                 hiddens=[],
                 shared_hiddens=[],
                 adver_hiddens=[],
                 recon_coef=1,
                 cross_coef=1,
                 integ_coef=1,
                 cycle_coef=1,
                 adversarial=True,
                 dropout=0.2,
                 pair_groups=[],
                 shared_encoder_output_activation='linear',
                 regularize_shared_encoder_last_layer=False,
                 device='cpu'):

        super(MultiScCAE, self).__init__()

        # save model parameters
        self.n_modal = len(x_dims)
        self.z_dim = z_dim
        self.h_dim = h_dim
        self.recon_coef = recon_coef
        self.cross_coef = self.cross_coef_init = cross_coef
        self.integ_coef = self.integ_coef_init = integ_coef
        self.cycle_coef = self.cycle_coef_init = cycle_coef
        self.adversarial = adversarial
        self.pair_groups = pair_groups
        self.device = device

        # TODO: do some assertions for the model parameters

        # create sub-modules
        self.encoders = [MLP(x_dim, h_dim, hiddens, output_activation='leakyrelu',
                             dropout=dropout, batch_norm=True, regularize_last_layer=True) for x_dim in x_dims]
        self.decoders = [MLP(h_dim, x_dim, hiddens[::-1], dropout=dropout, batch_norm=True) for x_dim in x_dims]
        self.shared_encoder = MLP(h_dim + self.n_modal, z_dim, shared_hiddens, output_activation=shared_encoder_output_activation,
                                  dropout=dropout, batch_norm=True, regularize_last_layer=regularize_shared_encoder_last_layer)
        self.shared_decoder = MLP(z_dim + self.n_modal, h_dim, shared_hiddens[::-1], output_activation='leakyrelu',
                                  dropout=dropout, batch_norm=True, regularize_last_layer=True)
        self.adversarial_discriminator = MLP(z_dim, self.n_modal, adver_hiddens, dropout=dropout, batch_norm=True, regularize_last_layer=False)

        # register sub-modules
        for i, (enc, dec) in enumerate(zip(self.encoders, self.decoders)):
            self.add_module(f'encoder-{i}', enc)
            self.add_module(f'decoder-{i}', dec)

        self = self.to(device)
    
    def get_nonadversarial_params(self):
        params = []
        for enc in self.encoders:
            params.extend(list(enc.parameters()))
        for dec in self.decoders:
            params.extend(list(dec.parameters()))
        params.extend(list(self.shared_encoder.parameters()))
        params.extend(list(self.shared_decoder.parameters()))
        return params
    
    def get_adversarial_params(self):
        return list(self.adversarial_discriminator.parameters())
    
    def warmup_mode(self, on=True):
        self.cross_coef = self.cross_coef_init * (not on)
        self.integ_coef = self.integ_coef_init * (not on)
        self.cycle_coef = self.cycle_coef_init * (not on)

    def encode(self, x, i):
        h = self.x_to_h(x, i)
        z = self.h_to_z(h, i)
        return z 

    def decode(self, z, i):
        h = self.z_to_h(z, i)
        x = self.h_to_x(h, i)
        return x
    
    def to_latent(self, x, i):
        return self.encode(x, i)
    
    def x_to_h(self, x, i):
        return self.encoders[i](x)
    
    def h_to_z(self, h, i):
        c = self.modal_vector(i).repeat(h.size(0), 1)
        z = self.shared_encoder(torch.cat([h, c], dim=1))
        return z
    
    def z_to_h(self, z, i):
        c = self.modal_vector(i).repeat(z.size(0), 1)
        h = self.shared_decoder(torch.cat([z, c], dim=1))
        return h
    
    def h_to_x(self, h, i):
        x = self.decoders[i](h)
        return x
    
    def adversarial_loss(self, z, i):
        y = self.modal_vector(i).repeat(z.size(0), 1).argmax(dim=1)
        y_pred = self.adversarial_discriminator(z)
        return nn.CrossEntropyLoss()(y_pred, y)
    
    def forward(self, xs, pair_masks):
        # encoder and decoder
        zs = [self.encode(x, i) for i, x in enumerate(xs)]
        rs = [self.decode(z, i) for i, z in enumerate(zs)]

        self.loss, losses = self.calc_loss(xs, rs, zs, pair_masks)
        self.adv_loss, adv_losses = self.calc_adv_loss(zs)
        
        return rs, self.loss - self.adv_loss, {**losses, **adv_losses}

    def calc_loss(self, xs, rs, zs, pair_masks):
        # reconstruction loss for each modality, seaprately
        recon_loss = sum([nn.MSELoss()(r, x) for x, r in zip(xs, rs)])

        # losses between modalities
        cross_loss = 0
        integ_loss = 0
        cycle_loss = 0

        for i, (xi, zi, pmi) in enumerate(zip(xs, zs, pair_masks)):
            for j, (xj, zj, pmj) in enumerate(zip(xs, zs, pair_masks)):
                if i == j:
                    continue
                rij = self.decode(zi, j)
                zij = self.to_latent(rij, j)

                cycle_loss += nn.MSELoss()(zi, zij)

                if self.pair_groups[i] is not None and self.pair_groups[i] == self.pair_groups[j]:
                    xj_paired, xj_unpaired = xj[pmj == 1], xj[pmj == 0]
                    zj_paired, zj_unpaired = zj[pmj == 1], zj[pmj == 0]
                    zi_paired, zi_unpaired = zi[pmi == 1], zi[pmi == 0]
                    rij_paired, rij_unpaired = rij[pmi == 1], rij[pmi == 0]

                    # unpaired losses
                    if len(zi_unpaired) > 0 and len(zj_unpaired) > 0:
                        integ_loss += MMD()(zi_unpaired, zj_unpaired)
                    if len(rij_unpaired) > 0 and len(xj_unpaired) > 0:
                        cross_loss += MMD()(rij_unpaired, xj_unpaired)

                    # paired losses
                    if len(zi_paired) > 0 and len(zj_paired) > 0:
                        integ_loss += nn.MSELoss()(zi_paired, zj_paired)
                    if len(rij_paired) > 0 and len(xj_paired) > 0:
                        cross_loss += nn.MSELoss()(rij_paired, xj_paired)
                else:
                    integ_loss += MMD()(zi, zj)
                    cross_loss += MMD()(rij, xj)
        
        return self.recon_coef * recon_loss + \
               self.cross_coef * cross_loss + \
               (not self.adversarial) * self.integ_coef * integ_loss + \
               self.cycle_coef * cycle_loss, {
                   'recon': recon_loss,
                   'cross': cross_loss,
                   'integ': integ_loss,
                   'cycle': cycle_loss
                }
    
    def calc_adv_loss(self, zs):
        loss = sum([self.adversarial_loss(z, i) for i, z in enumerate(zs)])
        return self.adversarial * self.integ_coef * loss, {'adver': loss}
    
    def modal_vector(self, i):
        return F.one_hot(torch.tensor([i]).long(), self.n_modal).float().to(self.device)

    def backward(self):
        (self.loss - self.adv_loss).backward()
    
    def backward_adv(self):
        self.adv_loss.backward()
    
    def test(self, *xs):
        outputs, loss, losses = self.forward(*xs)
        return loss, losses

    def integrate(self, x, i, j=None):
        zi = self.to_latent(x, i)
        return zi