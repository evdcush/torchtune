# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import pytest
import torch

from tests.test_utils import (
    _IdentityMapDataset,
    get_pet_launch_config,
    skip_if_cuda_not_available,
)
from torch.distributed import launcher
from torch.utils.data import Dataset, DistributedSampler, IterableDataset
from torchtune.utils import CheckpointableDataLoader


class _DummyIterableDataset(IterableDataset):
    def __iter__(self):
        yield 1


class TestCheckpointableDataLoader:
    def test_single_process_dataloader_checkpoint(self):
        dataset = _IdentityMapDataset(10)
        sampler = DistributedSampler(dataset, num_replicas=1, rank=0, shuffle=False)
        dataloader = CheckpointableDataLoader(
            dataset, batch_size=1, shuffle=None, sampler=sampler
        )
        state = dataloader.state_dict()
        assert state[CheckpointableDataLoader._SKIP_INDEX_KEY] == 0

        it = iter(dataloader)

        state = dataloader.state_dict()
        assert state[CheckpointableDataLoader._SKIP_INDEX_KEY] == 0
        for _ in range(3):
            next(it)

        state = dataloader.state_dict()
        assert state[CheckpointableDataLoader._SKIP_INDEX_KEY] == 3

        dataloader2 = CheckpointableDataLoader(
            dataset, batch_size=1, shuffle=None, sampler=sampler
        )
        dataloader2.load_state_dict(state)
        it = iter(dataloader2)
        data = next(it)

        assert data == 3
        state = dataloader2.state_dict()
        assert state[CheckpointableDataLoader._SKIP_INDEX_KEY] == 4

        # Creating new iterator should reset state of the dataloader
        for _ in range(2):
            it = iter(dataloader2)
            data = next(it)
            assert data == 0
            state = dataloader2.state_dict()
            assert state[CheckpointableDataLoader._SKIP_INDEX_KEY] == 1

    def test_state_change_on_load_state_dict(self):
        dataset = _IdentityMapDataset(10)
        sampler = DistributedSampler(dataset, num_replicas=1, rank=0, shuffle=False)
        dataloader = CheckpointableDataLoader(
            dataset, batch_size=1, shuffle=None, sampler=sampler
        )
        state = dataloader.state_dict()

        it = iter(dataloader)
        next(it)

        state = dataloader.state_dict()
        assert state[CheckpointableDataLoader._SKIP_INDEX_KEY] == 1

        dataloader2 = CheckpointableDataLoader(
            dataset, batch_size=1, shuffle=None, sampler=sampler
        )
        old_state = dataloader2.state_dict()
        assert old_state[CheckpointableDataLoader._SKIP_INDEX_KEY] == 0
        dataloader2.load_state_dict(state)
        state = dataloader2.state_dict()
        assert state[CheckpointableDataLoader._SKIP_INDEX_KEY] == 1

    @pytest.mark.parametrize("persistent_workers", [False, True])
    def test_data_with_sampler_shuffle(self, persistent_workers):
        dataset = _IdentityMapDataset(8)
        sampler = DistributedSampler(dataset, num_replicas=1, rank=0, shuffle=True)
        dataloader = CheckpointableDataLoader(
            dataset,
            batch_size=1,
            shuffle=None,
            sampler=sampler,
            num_workers=2,
            persistent_workers=persistent_workers,
            multiprocessing_context="forkserver",
            prefetch_factor=2,
        )

        expected_data = {
            0: [4, 0, 7, 3, 2, 5, 1, 6],
            1: [5, 4, 2, 6, 7, 3, 1, 0],
            2: [0, 4, 7, 2, 6, 5, 1, 3],
            3: [2, 4, 3, 5, 1, 0, 6, 7],
        }

        for epoch in range(3):
            sampler.set_epoch(epoch)
            for idx, data in enumerate(iter(dataloader)):
                assert data == expected_data[epoch][idx]
                if epoch == 2 and idx == 3:
                    break

        # Simulate taking a checkpoint after iterations
        state = dataloader.state_dict()

        sampler = DistributedSampler(dataset, num_replicas=1, rank=0, shuffle=True)
        dataloader2 = CheckpointableDataLoader(
            dataset,
            batch_size=1,
            shuffle=None,
            sampler=sampler,
            num_workers=2,
            persistent_workers=persistent_workers,
            multiprocessing_context="forkserver",
            prefetch_factor=2,
        )
        dataloader2.load_state_dict(state)
        sampler.set_epoch(2)

        # Point check to see if returned data is as expected
        it = iter(dataloader2)
        idx = 4
        # Iterate through the rest of the epoch
        for data in it:
            assert data == expected_data[2][idx]
            idx += 1

        # Start another iteration of new epoch to ensure it iterates through
        # full dataset
        sampler.set_epoch(3)
        actual_data = []
        for data in iter(dataloader2):
            actual_data.append(data)
        assert actual_data == expected_data[3]

    @pytest.mark.parametrize(
        "num_workers, persistent_workers, multiprocessing_context",
        [
            (0, None, None),
            (2, False, "fork"),
            (2, True, "fork"),
            (2, False, "forkserver"),
            (2, True, "forkserver"),
            (2, False, "spawn"),
            (2, True, "spawn"),
        ],
    )
    def test_shuffle_off_larger_batch_size(
        self, num_workers, persistent_workers, multiprocessing_context
    ):
        dataset = _IdentityMapDataset(4)
        sampler = DistributedSampler(dataset, num_replicas=1, rank=0, shuffle=False)

        dataloader = CheckpointableDataLoader(
            dataset,
            batch_size=2,
            shuffle=None,
            sampler=sampler,
            num_workers=num_workers,
            multiprocessing_context=multiprocessing_context,
            persistent_workers=persistent_workers,
        )

        it = iter(dataloader)
        data = next(it)
        assert torch.equal(data, torch.tensor([0, 1]))
        state = dataloader.state_dict()
        assert state[CheckpointableDataLoader._SKIP_INDEX_KEY] == 1
        data = next(it)
        assert torch.equal(data, torch.tensor([2, 3]))
        state = dataloader.state_dict()
        assert state[CheckpointableDataLoader._SKIP_INDEX_KEY] == 2

        dataloader2 = CheckpointableDataLoader(
            dataset, batch_size=2, shuffle=None, sampler=sampler
        )
        dataloader2.load_state_dict(state)
        it = iter(dataloader2)
        with pytest.raises(StopIteration):
            next(it)

    def test_dataloader_init_value_errors(self):
        dataset = _IdentityMapDataset(10)
        iterable_dataset = _DummyIterableDataset()

        with pytest.raises(
            ValueError,
            match="CheckpointableDataLoader currently supports only map-style dataset. Received an IterableDataset instead.",
        ):
            CheckpointableDataLoader(iterable_dataset)
        with pytest.raises(
            ValueError,
            match=r"CheckpointableDataLoader currently supports only DistributedSampler. Received a sampler of type .*",
        ):
            CheckpointableDataLoader(dataset)

    def test_seed_not_same_on_resume(self):
        dataset = _IdentityMapDataset(5)
        sampler = DistributedSampler(dataset, seed=5, num_replicas=1, rank=0)
        dataloader = CheckpointableDataLoader(
            dataset, batch_size=1, shuffle=None, sampler=sampler
        )
        state = dataloader.state_dict()
        assert state[CheckpointableDataLoader._DISTRIBUTED_SAMPLER_SHUFFLE_SEED] == 5

        # Modify the state and ensure assertion error is thrown on load
        state[CheckpointableDataLoader._DISTRIBUTED_SAMPLER_SHUFFLE_SEED] = 10
        with pytest.raises(
            ValueError, match=r"On dataloader state load, sampler seed is different.*"
        ):
            dataloader.load_state_dict(state)

    def test_state_contains_expected_keys(self):
        dataset = _IdentityMapDataset(5)
        sampler = DistributedSampler(dataset, seed=5, num_replicas=1, rank=0)
        dataloader = CheckpointableDataLoader(
            dataset, batch_size=1, shuffle=None, sampler=sampler
        )
        # Perform one batch fetch
        it = iter(dataloader)
        next(it)

        # Check that the state contains the expected keys
        assert dataloader.state_dict() == {
            CheckpointableDataLoader._DISTRIBUTED_SAMPLER_SHUFFLE_SEED: 5,
            CheckpointableDataLoader._SKIP_INDEX_KEY: 1,
        }

    def _test_distributed_data_loader_save_load(self) -> None:
        torch.distributed.init_process_group(backend="gloo")
        dataset = _IdentityMapDataset(16 * torch.distributed.get_world_size())
        # Create a sampler and dataloader that we will checkpoint
        ws, rank = torch.distributed.get_world_size(), torch.distributed.get_rank()
        sampler = DistributedSampler(
            dataset, num_replicas=ws, rank=rank, shuffle=True, seed=5
        )
        dataloader = CheckpointableDataLoader(
            dataset,
            batch_size=1,
            shuffle=None,
            sampler=sampler,
        )

        # Iterate through the DL to save the expected order of samples.
        dataloader_iterator = iter(dataloader)
        data_for_rank = []
        for x in dataloader_iterator:
            data_for_rank.append(x)

        # Now check if we pause training and restore into a different dataloader, we should get the same data.
        restore_data_for_rank = []
        # Now checkpoint the dataloader in the middle of iterating
        dataloader_iterator = iter(dataloader)
        for i, x in enumerate(dataloader_iterator):
            restore_data_for_rank.append(x)
            if i == 7:
                break
        state = dataloader.state_dict()

        # Create a new dataloade / sampler pair and load the state in.
        restore_sampler = DistributedSampler(
            dataset, num_replicas=ws, rank=rank, shuffle=True, seed=5
        )
        restore_dataloader = CheckpointableDataLoader(
            dataset,
            batch_size=1,
            shuffle=None,
            sampler=sampler,
        )
        restore_dataloader.load_state_dict(state)
        # Now upon iterating the dataloader, we should resume at the appropriate index and get the same data
        # as when we did a full iteration before checkpointing.
        restore_dataloader_iterator = iter(restore_dataloader)
        for x in restore_dataloader_iterator:
            restore_data_for_rank.append(x)

        assert (
            restore_data_for_rank == data_for_rank
        ), f"mismatch: {data_for_rank} vs {restore_data_for_rank} on {torch.distributed.get_rank()}"

    @skip_if_cuda_not_available
    def test_dist_dataloader_save_load(self) -> None:
        lc = get_pet_launch_config(nproc=4)
        print(f"RV -- launching ---")
        launcher.elastic_launch(
            lc, entrypoint=self._test_distributed_data_loader_save_load
        )()
