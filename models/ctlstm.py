"""
Neural network models for point processes.

@author: manifold
"""
import numpy as np
import torch
from torch import Tensor
from torch import nn
from torch.nn.utils.rnn import PackedSequence
from typing import Tuple, List


class HawkesLSTM(nn.Module):
    """
    A continuous-time LSTM, defined according to Eisner & Mei's article
    https://arxiv.org/abs/1612.09328
    """

    def __init__(self, input_size: int, hidden_size: int):
        super(HawkesLSTM, self).__init__()
        self.process_dim = input_size
        self.trained_epochs = 0
        input_size += 1
        self.input_size = input_size  # embedding input size
        self.hidden_size = hidden_size
        self.embed = nn.Embedding(input_size, self.process_dim, padding_idx=self.process_dim)
        self.input_g = nn.Sequential(
            nn.Linear(self.process_dim + hidden_size, hidden_size),
            nn.Sigmoid()
        )
        self.forget_g = nn.Sequential(
            nn.Linear(self.process_dim + hidden_size, hidden_size),
            nn.Sigmoid()
        )
        self.output_g = nn.Sequential(
            nn.Linear(self.process_dim + hidden_size, hidden_size),
            nn.Sigmoid()
        )
        self.input_target = nn.Sequential(
            nn.Linear(self.process_dim + hidden_size, hidden_size),
            nn.Sigmoid()
        )
        self.forget_target = nn.Sequential(
            nn.Linear(self.process_dim + hidden_size, hidden_size),
            nn.Sigmoid()
        )
        # activation will be tanh
        self.z_gate = nn.Sequential(
            nn.Linear(self.process_dim + hidden_size, hidden_size),
            nn.Tanh()
        )
        # Cell decay factor, identical for all hidden dims
        self.decay_layer = nn.Sequential(
            nn.Linear(self.process_dim + hidden_size, hidden_size),
            nn.Softplus(beta=8.)
        )
        # activation for the intensity
        self.intensity_layer = nn.Sequential(
            nn.Linear(hidden_size, self.process_dim, bias=False),  # no bias in the model
            nn.Softplus(beta=5.)
        )

    def init_hidden(self, batch_size: int = 1, device=None) -> Tuple[Tensor, Tensor]:
        """
        Initialize the hidden state and the cell state.
        The initial cell state target is equal to the initial cell state.
        The first dimension is the batch size.

        Returns:
            (hidden, cell_state)
        """
        (h0, c0) = (torch.zeros(batch_size, self.hidden_size),
                    torch.zeros(batch_size, self.hidden_size))
        if device:
            h0 = h0.to(device)
            c0 = c0.to(device)
        return h0, c0

    def forward(self, seq_dt: PackedSequence, seq_types: PackedSequence,
                h0: Tensor, c0: Tensor
                ) -> Tuple[List[Tensor], List[Tensor], List[Tensor], List[Tensor], List[Tensor], List[Tensor]]:
        """
        Forward pass of the LSTM network.

        Computes the network parameters for the next interval :math:`(t_i,t_{i+1}]` from the input states given by
        at time :math:`t_{i}`, right before the update.

        Args:
            seq_dt: time until next event
                Shape: seq_len * batch
            seq_types: one-hot encoded event sequence types
                Shape: seq_len * batch * input_size
            h0: initial hidden state
            c0: initial cell state

        Returns:
            outputs: computed by the output gates
            hidden_ti: decayed hidden states hidden state
            cells: cell states
            cell_targets: target cell states
            decays: decay parameters for each interval
        """
        h_t = h0  # continuous hidden state
        c_t = c0  # continuous cell state
        c_target = c0  # cell state target
        hiddens = []  # full, updated hidden states
        hiddens_decayed = []  # decayed hidden states, directly used in log-likelihood computation
        outputs = []  # output from each LSTM pass
        cells = []  # cell states at event times
        cell_targets = []  # target cell states for each interval
        decays = []  # decays computed at each event
        max_seq_length = len(seq_dt.batch_sizes)
        beg_index = 0
        # loop over all events
        for i in range(max_seq_length):
            batch_size = seq_dt.batch_sizes[i]
            h_t = h_t[:batch_size]
            c_t = c_t[:batch_size]
            c_target = c_target[:batch_size]
            dt_sub_batch = seq_dt.data[beg_index:(beg_index + batch_size)]
            types_sub_batch = seq_types.data[beg_index:(beg_index + batch_size)]

            # Update the hidden states and LSTM parameters following the equations
            x = self.embed(types_sub_batch)
            h_i, cell_i, c_target, output, decay_i = self.update_states(
                x, h_t, c_t, c_target
            )
            c_t: Tensor = (
                    c_target + (cell_i - c_target) *
                    torch.exp(-decay_i * dt_sub_batch[:, None])
            )
            h_t: Tensor = output * torch.tanh(c_t)  # decayed hidden state just before next event

            outputs.append(output)  # record it
            decays.append(decay_i)
            cells.append(cell_i)  # record it
            hiddens.append(h_i)  # record it
            cell_targets.append(c_target)  # record
            hiddens_decayed.append(h_t)  # record it
        return hiddens, hiddens_decayed, outputs, cells, cell_targets, decays

    def update_states(self, x: Tensor, h_t: Tensor, c_t: Tensor, c_target: Tensor
                      ) -> Tuple[Tensor, Tensor, Tensor, Tensor, Tensor]:
        """
        Compute the updated LSTM paramters.

        Args:s
            x:
            h_t:
            c_t:
            c_target:

        Returns:
            h_i: just-updated hidden state
            h_t: hidden state just before next event
            cell_i: just-updated cell state
            c_t: cell state decayed to before next event
            c_target_i: cell state target before the next event
            output: LSTM output
            decay_i: rate of decay for the cell state
        """
        v = torch.cat((x, h_t), dim=1)
        inpt = self.input_g(v)
        forget = self.forget_g(v)
        input_target = self.input_target(v)
        forget_target = self.forget_target(v)
        output = self.output_g(v)  # compute the LSTM network output
        # Not-quite-c
        z_i = self.z_gate(v)
        # Compute the decay parameter
        decay = self.decay_layer(v)
        # Update the cell state to c(t_i+)
        c_i = forget * c_t + inpt * z_i

        h_i: Tensor = output * torch.tanh(c_i)  # hidden state just after event

        # Update the cell state target
        c_target: Tensor = forget_target * c_target + input_target * z_i
        # Decay the cell state to its value before the known next event at t+dt
        # used for the next pass in the loop
        return h_i, c_i, c_target, output, decay

    def compute_intensity(self, output: Tensor, c_i: Tensor, c_target_i: Tensor, decay: Tensor, dt: Tensor) -> Tensor:
        """
        Compute the intensity at time :math:`t`, given the LSTM parameters on the interval
        :math:`(t_{i-1}, t_i]` and the elapsed time :math:`t-t_{i-1}` since the last
        event.

        Args:
            dt: time elapsed since last event
                Shape: batch
            output: LSTM cell output
                Shape: seq_length * batch * hidden_dim
            c_i: LSTM cell state just after the last event
            c_target_i: cell state target
            decay: decay speed parameter

        Shape:
            batch_size * hidden_dim
        """
        # Get the current continuous-time cell state
        c_t = (
                c_target_i + (c_i - c_target_i) *
                torch.exp(-decay * dt)
        )
        # Compute the hidden state
        h_t: Tensor = output * torch.tanh(c_t)
        if h_t.ndimension() > 2:
            return self.intensity_layer(h_t.transpose(1, 2)).transpose(1, 2)
        return self.intensity_layer(h_t)

    def compute_loss(self, seq_times: Tensor, seq_onehot_types: Tensor, batch_sizes: Tensor, hiddens_ti: List[Tensor],
                     cells: List[Tensor], cell_targets: List[Tensor], outputs: List[Tensor],
                     decays: List[Tensor], tmax: float) -> Tensor:
        """
        Compute the negative log-likelihood as a loss function.
        
        Args:
            seq_times: event occurrence timestamps
            seq_onehot_types: types of events in the sequence, one hot encoded
            batch_sizes: batch sizes for each event sequence tensor, by length
            hiddens_ti: hidden states just before the events occur.
            cells: entire cell state history
            cell_targets: cell state target values history
            outputs: entire output history
            decays: entire decay history
            tmax: temporal horizon

        Returns:
            log-likelihood of the event times under the learned parameters

        Shape:
            one-element tensor
        """
        n_batch = seq_times.size(0)
        n_times = len(hiddens_ti)
        dt_seq: Tensor = seq_times[:, 1:] - seq_times[:, :-1]
        device = dt_seq.device
        # Get the intensity process
        intens_at_evs: Tensor = [
            self.intensity_layer(hiddens_ti[i])
            for i in range(1, n_times)  # do not count the 0-th or End-of-sequence events
        ]  # intensities just before the events occur
        # shape batch * N * input_dim
        intens_at_evs = nn.utils.rnn.pad_sequence(
            intens_at_evs, padding_value=1.0)  # pad with 0 to get rid of the non-events
        log_intensities = intens_at_evs.log()  # log intensities
        # get the intensities of the types which are relevant to each event
        # multiplying by the one-hot seq_types tensor sets the non-relevant intensities to 0
        intens_ev_times_filtered = (log_intensities * seq_onehot_types[:, 1:-1]).sum(dim=2)
        # reduce on the type dim. (dropping the 0s in the process), then
        # reduce the log-intensities on seq_times dim.
        # shape (batch_size,)
        log_sum = intens_ev_times_filtered.sum(dim=1)

        # COMPUTE INTEGRAL TERM
        # Computed using Monte Carlo method

        # Take uniform time samples inside of each inter-event interval
        # seq_times: Tensor = torch.cat((seq_times, tmax*torch.ones_like(seq_times[-1:, :])))
        # dt_sequence = seq_times[1:] - seq_times[:-1]
        n_mc_samples = 10
        # shape N * batch * M_mc
        taus = torch.rand(n_batch, n_times, n_mc_samples).to(device)
        taus: Tensor = dt_seq.unsqueeze(-1) * taus  # inter-event times samples
        taus.unsqueeze_(2)
        intens_at_samples = [
            self.compute_intensity(
                outputs[i].unsqueeze(-1), cells[i].unsqueeze(-1),
                cell_targets[i].unsqueeze(-1), decays[i].unsqueeze(-1),
                taus[:batch_sizes[i], i])
            for i in range(n_times)
        ]
        intens_at_samples = nn.utils.rnn.pad_sequence(
            intens_at_samples, padding_value=0.0)  # shape N * batch * K * MC
        total_intens_samples: Tensor = intens_at_samples.sum(dim=2)  # shape N * batch * MC
        partial_integrals: Tensor = dt_seq * total_intens_samples.mean(dim=2)
        integral_ = partial_integrals.sum(dim=1)
        res = (- log_sum + integral_).mean()  # mean on batch dim
        return res


class HawkesLSTMGen:
    """
    Sequence generator for the CT-LSTM model.
    """

    def __init__(self, model: HawkesLSTM, record_intensity: bool = True):
        self.model = model
        self.process_dim = model.input_size - 1  # process dimension
        print("Process model dim:\t{}\tHidden units:\t{}".format(self.process_dim, model.hidden_size))
        self.event_times = []
        self.event_types = []
        self.event_intens = []
        self.decay_hist: List[Tensor] = []
        self.intens_hist: List[Tensor] = []
        self.all_times_ = []
        self.record_intensity: bool = record_intensity

    def restart_sequence(self):
        self.event_times = []
        self.event_types = []
        self.event_intens = []
        self.decay_hist = []
        self.intens_hist = []
        self.all_times_ = []

    def generate_sequence(self, tmax: float, record_intensity: bool = False):
        """
        Generate an event sequence on the interval [0, tmax].

        Args:
            tmax: maximum time.
            record_intensity (bool): whether or not to record the intensity (e.g. for plotting)
        """
        self.restart_sequence()
        model = self.model
        model.eval()
        if record_intensity is None:
            record_intensity = self.record_intensity
        with torch.no_grad():
            last_t = 0.0
            s = torch.zeros(1)
            h0, c0 = model.init_hidden()
            h0.normal_(std=0.1)
            c0.normal_(std=0.1)
            h_t = h0
            c_t = c0
            c_target = c0
            # Compute the first hidden states from the noise, at t = 0
            x0 = torch.zeros(1, 1)  # the starter event has an embedding of [ 0. ]
            h_t, c_t, c_target, output, decay = model.update_states(
                x0, h_t, c_t, c_target
            )
            intens = model.intensity_layer(h_t)
            # Record everything
            self.event_times.append(last_t)
            self.event_types.append(self.process_dim)
            self.event_intens.append(intens.numpy())
            self.decay_hist.append(decay.numpy())
            self.all_times_.append(last_t)
            self.intens_hist.append(intens.numpy())
            max_lbda = intens

            while last_t <= tmax:
                u1 = torch.rand(1)
                ds: torch.Tensor = -1. / max_lbda * np.log(u1)
                u = s.item()
                du = ds.item() / 10  # sampling interval for intensity record
                s: Tensor = s + ds.item()  # Increment s
                if s > tmax:
                    break
                if record_intensity:
                    c_u = c_t
                    while u < s.item():
                        self.all_times_.append(u)
                        c_u = c_target + (c_u - c_target)*torch.exp(-decay*du)
                        h_u = output * torch.tanh(c_u)
                        lbda_u = model.intensity_layer(h_u)
                        self.intens_hist.append(lbda_u)
                        u += du
                # Compute the current intensity
                # Compute what the cell state at s is
                # Decay the hidden state
                c_t = c_target + (c_t - c_target)*torch.exp(-decay*ds)
                h_t = output * torch.tanh(c_t)
                # Apply intensity layer
                intens: Tensor = self.model.intensity_layer(h_t)
                self.all_times_.append(s.item())  # record this time and intensity value
                self.intens_hist.append(intens.numpy())
                u2 = np.random.rand()  # random in [0,1]
                total_intens = intens.sum(dim=1, keepdim=True)
                ratio = intens/max_lbda
                if u2 <= ratio:
                    # shape 1 * K
                    # probability distribution for the types
                    weights: Tensor = intens / total_intens  # ratios of types intensities to aggregate
                    res = torch.multinomial(weights, 1)
                    k = res.item()
                    # accept
                    x = model.embed(res[0])
                    # Bump the hidden states
                    h_t, c_t, c_target, output, decay = model.update_states(
                        x, h_t, c_t, c_target
                    )
                    max_lbda = self.update_max_lambda(output, c_t, c_target)
                    last_t = s.item()
                    self.all_times_.append(last_t)  # record the time and intensity a second time
                    self.intens_hist.append(intens)
                    self.decay_hist.append(decay)
                    self.event_intens.append(intens)
                    self.event_types.append(k)
                    self.event_times.append(last_t)

    def update_max_lambda(self, output, c_t: Tensor, c_target: Tensor) -> torch.Tensor:
        """
        Considering current time is s and knowing the last event, find a new maximum value of the intensity.
        
        Each term in the sum defining pre_lambda (intensity before activation) is of the form
        .. math::
            o_k\tanh([c_{i+1}]_k + ([c_{i+1}]_k - [\bar c_{i+1}]_k)\exp(-\delta_k(t - t_i)))
        """
        w_alpha = self.model.intensity_layer[0].weight.data
        cell_diff = c_t - c_target
        mult_prefix = w_alpha * output
        pos_prefactor = mult_prefix > 0
        pos_decr = pos_prefactor & (cell_diff >= 0)
        p1 = torch.dot(mult_prefix[pos_decr], torch.tanh(c_t[pos_decr]))
        pos_incr = pos_prefactor & (cell_diff < 0)
        p2 = torch.dot(mult_prefix[pos_incr], torch.tanh(c_target[pos_incr]))
        neg_decr = ~pos_prefactor & (cell_diff >= 0)
        p3 = torch.dot(mult_prefix[neg_decr], torch.tanh(c_target[neg_decr]))
        neg_incr = ~pos_prefactor & (cell_diff < 0)
        p4 = torch.dot(mult_prefix[neg_incr], torch.tanh(c_t[neg_incr]))
        lbda_tilde = p1 + p2 + p3 + p4
        # compute the intensity using the Softplus inside the intensity layer of the model
        res = self.model.intensity_layer[1](lbda_tilde)
        return res

    def plot_events_and_intensity(self, model_name: str = None):
        import matplotlib.pyplot as plt
        gen_seq_times = self.event_times
        gen_seq_types = self.event_types
        sequence_length = len(gen_seq_times)
        print("no. of events: {}".format(sequence_length))
        evt_times = np.array(gen_seq_times)
        evt_types = np.array(gen_seq_types)
        fig, ax = plt.subplots(1, 1, sharex='all', dpi=100,
                               figsize=(10, 4.5))
        ax: plt.Axes
        inpt_size = self.process_dim+1
        ax.set_xlabel('Time $t$ (s)')
        intens_hist = np.stack(self.intens_hist)[:, 0]
        labels = ["type {}".format(i) for i in range(self.process_dim)]
        for y, lab in zip(intens_hist.T, labels):
            ax.plot(self.all_times_, y, linewidth=.7, label=lab)
        ax.set_ylabel(r"Intensities $\lambda^i_t$")
        title = "Event arrival times and intensities for generated sequence"
        if model_name:
            title += " ({})".format(model_name)
        ax.set_title(title)
        ylims = ax.get_ylim()
        ts_y = np.stack(self.event_intens)[:, 0]
        for k in range(inpt_size):
            mask = evt_types == k
            if k == self.process_dim:
                label = "start event".format(k)
                y = self.intens_hist[0]*np.ones_like(evt_times[mask])
            else:
                y = ts_y[mask, k]
                label = "type {} event".format(k)
            ax.scatter(evt_times[mask], y, s=9, zorder=5,
                       label=label, alpha=0.8)
            ax.vlines(evt_times[mask], ylims[0], ylims[1], linewidth=0.3, linestyles='--', alpha=0.5)
        ax.set_ylim(*ylims)
        ax.legend()
        fig.tight_layout()
        return fig

