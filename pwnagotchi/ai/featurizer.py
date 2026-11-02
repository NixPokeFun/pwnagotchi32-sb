import numpy as np

import pwnagotchi.mesh.wifi as wifi

MAX_EPOCH_DURATION = 1024


def describe():
    histogram_size = wifi.NumChannels

    # 1D shape (N,) instead of (1, N) for compatibility with older Gym/TF
    total_features = (
        histogram_size * 3 +   # aps, sta, peers histograms
        8                      # duration, inactive, active, missed, hops, deauths, assocs, handshakes
    )
    return histogram_size, (total_features,)


def featurize(state, step):
    tot_epochs = step + 1e-10
    tot_interactions = (state['num_deauths'] + state['num_associations']) + 1e-10

    return np.concatenate((
        state['aps_histogram'],
        state['sta_histogram'],
        state['peers_histogram'],
        [np.clip(state['duration_secs'] / MAX_EPOCH_DURATION, 0.0, 1.0)],
        [state['inactive_for_epochs'] / tot_epochs],
        [state['active_for_epochs'] / tot_epochs],
        [state['missed_interactions'] / tot_interactions],
        [state['num_hops'] / wifi.NumChannels],
        [state['num_deauths'] / tot_interactions],
        [state['num_associations'] / tot_interactions],
        [state['num_handshakes'] / tot_interactions],
    )).astype(np.float32)
