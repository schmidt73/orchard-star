########################################################################
# chain.py
#
# Contains the source code to parallelize Orchard.
########################################################################
import sys, os
from concurrent.futures import ProcessPoolExecutor
from queue import Empty
from multiprocessing import Manager
import numpy as np
from tqdm import tqdm

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "input"))
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "branch"))
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "sampling"))

from constants import MAX_SEED
from Branch import Branch
from actions_sampler import Actions_Sampler


def run_chain(model_type, 
              model_data, 
              branch_subset, 
              F_data, 
              generator, 
              progress_queue):
    """Runs a model instance within its own process
    
    Parameters
    ----------
    model_type : object
        an instance of one of the model types defined in model_types.py (BeamSearch, StochasticBeamSearch)
    model_data : dataclass
        a dataclass containing all of the information needed to setup the model
    branch_subset : list
        the list of branches to initialize the model with
    F_data : dataclass
        a dataclass containing the read count data for all supervariants
    generator : object
        a numpy default_rng object used for reproducible random sampling
    progress_queue : object
        a thread safe python queue 

    Returns
    -------
    list 
        sorted list of Branch objects ordered by their scores (likelihoods) for this instance of search
    int 
        number of partial solutions that were considered during this instance of search
    int
        number of partial solutions that were discarded during this instance of search
    """
    model = model_type(branch_subset, model_data, F_data, generator, progress_queue)
    model.search()
    return model.best_trees(), model.branches_explored(), model.branches_cut()


def run_chains_parallel(model_type, 
                        model_data, 
                        F_data,
                        setup_data):
    """Runs n_chains each of which run in its own process. Each chain runs the same
    model_type but each are seeded differently.
    
    Parameters
    ----------
    model_type : object
        an instance of one of the model types defined in model_types.py (BeamSearch, StochasticBeamSearch)
    model_data : dataclass
        a dataclass containing all of the information needed to setup the model
    F_data : dataclass
        a dataclass containing the read count data for all supervariants
    setup_data : dataclass
        a dataclass containing the information for how to parallelize the model

    Returns
    -------
    list 
        sorted list of Branch objects ordered by their scores (likelihoods)
    int 
        number of partial solutions that were considered during search
    int
        number of partial solutions that were discarded during search
    """
    # initialize everything we need to capture outputs from each chain we run
    futures = []
    best_trees = []
    branches_cut, branches_explored = 0, 0
    progress_queue = Manager().Queue() # progress queue to collect information about each 
    finished = np.full(setup_data.n_chains, False) # boolean array keeping track of which chains have completed
    n_nodes = len(F_data.V)
    n_samples = F_data.V.shape[1]
    # Progress bar assumes we'll receive beam_width * n_chains number of items into our progress_queue
    with tqdm(total=model_data.beam_width*setup_data.n_chains) as pbar:

        # Setup pool of processes to submit our n_chains to
        # Each chain runs the same model with a different seed and a different subset of the initial branches
        with ProcessPoolExecutor(max_workers=setup_data.poolsize) as ex:

            for i in range(setup_data.n_chains):
                # Submit jobs with different seeds, but all based on the original seed provided such that the results 
                # can be reproduced
                generator = np.random.default_rng((setup_data.seed + i + 1) % MAX_SEED)

                # initialize action sampler
                actions_sampler = Actions_Sampler(num_nodes=n_nodes, 
                                                  num_samples=n_samples, 
                                                  ignore_zero_probs=model_data.ignore_zero_probs,
                                                  force_monoprimary=model_data.force_monoprimary,
                                                  max_placements=model_data.max_placements)
                actions_sampler._compute_F_sum(F_data)
                actions_sampler._sample_node_order(generator, randomize=setup_data.randomize_nodes)

                futures.append(
                    ex.submit(run_chain, 
                              model_type, 
                              model_data, 
                              [Branch(size=n_nodes, samples=n_samples, actions_sampler=actions_sampler)], # empty starting branch
                              F_data,
                              generator,
                              progress_queue=progress_queue))

            # stay in loop until all n_chains have completed
            while not all(finished):
                
                for i, complete in enumerate(finished):
                    # check if completed
                    if not complete and futures[i].done():
                        # check for exception
                        exception = futures[i].exception()
                        if exception is not None:
                            print('%r generated an exception: %s' % (futures[i], exception))
                            raise exception

                        # process results
                        trees, _branches_explored, _branches_cut = futures[i].result()
                        best_trees += trees
                        branches_explored += _branches_explored
                        branches_cut += _branches_cut
                        finished[i] = True

                # update progress queue
                try:
                    progress_queue.get_nowait()
                    pbar.update()
                except Empty:
                    pass


    return sorted(best_trees), branches_explored, branches_cut