# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
"""Define the loop_tool environment."""
import logging
import time
from functools import reduce
from pathlib import Path
from typing import Optional, Tuple

import loop_tool_py as lt
import numpy as np

from compiler_gym.service import CompilationSession
from compiler_gym.service.proto import (
    Action,
    ActionSpace,
    Benchmark,
    Int64List,
    Observation,
    ObservationSpace,
    ScalarLimit,
    ScalarRange,
    ScalarRangeList,
)


class LoopToolCompilationSession(CompilationSession):
    """Represents an instance of an interactive loop_tool session."""

    compiler_version: str = "0.0.1"

    # keep it simple for now: 1 variable, 1 nest
    action_spaces = [
        ActionSpace(
            # shift around a single pre-split order, changing the size of splits
            name="simple",
            action=["toggle_mode", "up", "down", "toggle_thread"],
        ),
        ActionSpace(
            # potentially define new splits
            name="split",
            action=["toggle_mode", "up", "down", "toggle_thread", "split"],
        ),
    ]

    observation_spaces = [
        ObservationSpace(
            name="flops",
            scalar_double_range=ScalarRange(),
            deterministic=False,
            platform_dependent=True,
            default_value=Observation(
                scalar_double=0,
            ),
        ),
        ObservationSpace(
            name="action_state",
            int64_range_list=ScalarRangeList(
                # FIXME(bwasti): Dummy values.
                range=[
                    ScalarRange(
                        min=ScalarLimit(value=0),
                        max=ScalarLimit(value=10),
                    ),
                ]
            ),
            deterministic=True,
            platform_dependent=False,
            default_value=Observation(
                int64_list=Int64List(
                    # FIXME(bwasti): Dummy values.
                    value=[0]
                ),
            ),
        ),
    ]

    def __init__(
        self, working_directory: Path, action_space: ActionSpace, benchmark: Benchmark
    ):
        super().__init__(working_directory, action_space, benchmark)
        self.ir = lt.IR()
        self.var = self.ir.create_var("a")
        r0 = self.ir.create_node("read", [], [self.var])
        r1 = self.ir.create_node("read", [], [self.var])
        add = self.ir.create_node("add", [r0, r1], [self.var])
        w = self.ir.create_node("write", [add], [self.var])
        self.ir.set_inputs([r0, r1])
        self.ir.set_outputs([w])
        self.size = int(benchmark.uri.split("/")[-1])
        self.Ap = np.random.randn(self.size)
        self.Bp = np.random.randn(self.size)
        self.order = [(self.size, 0), (1, 0), (1, 0)]
        self.thread = [1, 0, 0]
        self.cursor = 0
        self.mode = "size"
        logging.info("Started a compilation session for %s", benchmark.uri)

    def resize(self, increment):
        """
        The idea is pull from or add to the parent loop.

        Three mutations possible to any size:
        A) x, y -> x + 1, 0
          remove the tail, increase loop size, shrink parent
        B) x, y -> x, 0
          only remove the tail, add to parent
        C) x, 0 -> x - 1, 0
          if no tail, shrink the loop size, increase parent

        note: this means tails can never exist on innermost loops. this makes good sense :)

        A)

        [(a, b), (x, y), ...k] -> [(a', b'), (x + 1, 0), ...k]
        a * (x * k + y) + b = a' * (x + 1) * k + b'
        a' = (a * (x * k + y) + b) // ((x + 1) * k)
        b' = "                   " %  "           "

        B)

        [(a, b), (x, y), ...k] -> [(a', b'), (x, 0), ...k]
        a * (x * k + y) + b = a' * (x) * k + b'
        a' = (a * (x * k + y) + b) // ((x) * k)
        b' = "                   " %  "           "

        C)

        [(a, b), (x, y), ...k] -> [(a', b'), (x - 1, 0), ...k]
        a * (x * k + y) + b = a' * (x - 1) * k + b'
        a' = (a * (x * k + y) + b) // ((x - 1) * k)
        b' = "                   " %  "           "

        example interaction model:
        1. cursor = 1        [1024, 1, 1]
        2. up                [512, 2, 1]
        3. up                [(341,1), 3, 1]
        4. up                [256, 4, 1]
        5. cursor = 2, up    [256, 2, 2]
        6. up                [256, (1, 1), 3]
        7. cursor = 1, down  [(341, 1), 1, 3]
        8. cursor = 2, down  [(341, 1), (1, 1), 2]
        9. cursor = 1, down  [512, 1, 2]"""
        if self.cursor == 0:
            return
        parent_size = self.order[self.cursor - 1]
        a = parent_size[0]
        b = parent_size[1]
        size = self.order[self.cursor]
        x = size[0]
        y = size[1]

        def lam(v, x):
            return v * x[0] + x[1]

        k = reduce(lam, self.order[self.cursor + 1 :], 1)
        if increment == -1 and y:
            increment = 0
        if x + increment < 1:
            return
        n = a * x * k + b
        d = (x + increment) * k
        a_ = n // d
        b_ = n % d
        self.order[self.cursor - 1] = (a_, b_)
        self.order[self.cursor] = (x + increment, 0)
        end_size = reduce(lam, self.order, 1)
        assert end_size == self.size

    def apply_action(self, action: Action) -> Tuple[bool, Optional[ActionSpace], bool]:
        logging.info("Applied action %d", action.action)
        if action.action < 0 or action.action > len(self.action_spaces[0].action):
            raise ValueError("Out-of-range")

        act = self.action_spaces[0].action[action.action]
        # print("doing", act)
        if self.mode not in ["size", "select"]:
            raise RuntimeError("Invalid mode set: {}".format(self.mode))
        if act == "toggle_mode":
            if self.mode == "size":
                self.mode = "select"
            elif self.mode == "select":
                self.mode = "size"
        if act == "toggle_thread":
            self.thread[self.cursor] = not self.thread[self.cursor]
        if act == "down":
            # always loop around
            if self.mode == "size":
                self.resize(-1)
            elif self.mode == "select":
                next_cursor = (self.cursor - 1) % len(self.order)
                self.cursor = next_cursor
        if act == "up":
            # always loop around
            if self.mode == "size":
                self.resize(1)
            elif self.mode == "select":
                next_cursor = (self.cursor + 1) % len(self.order)
                self.cursor = next_cursor

        # print(self.mode, self.cursor, self.order, self.thread)
        return False, None, False

    def flops(self):
        print(self.ir)
        for n in self.ir.nodes:
            print(n)
            o = [(self.var, k) for k in self.order]
            print(o)
            self.ir.set_order(n, o)
        loop_tree = lt.LoopTree(self.ir)
        print(loop_tree)
        parallel = set()
        t = loop_tree.roots[0]
        for b in self.thread:
            if b:
                parallel.add(t)
            t = loop_tree.children(t)[0]
        try:
            c = lt.CompiledCuda(loop_tree, parallel)
        except Exception as e:
            print(str(e))
        A = lt.Tensor(self.size)
        B = lt.Tensor(self.size)
        C = lt.Tensor(self.size)
        A.set(self.Ap)
        B.set(self.Bp)
        iters = 10000
        # warmup
        for i in range(50):
            c([A, B, C])
        # return 100
        t = time.time()
        for i in range(iters - 1):
            c([A, B, C], False)
        c([A, B, C])
        t_ = time.time()
        flops = self.size * iters / (t_ - t) / 1e9
        return flops

    def get_observation(self, observation_space: ObservationSpace) -> Observation:
        # TODO populate
        if observation_space.name == "action_state":
            observation = Observation()
            # split cursor, size cursor
            observation.int64_list.value[:] = [self.cursor, self.order[self.cursor]]
            return observation
        elif observation_space.name == "flops":
            try:
                return Observation(scalar_double=self.flops())
            except Exception as e:
                print(str(e))
        else:
            raise KeyError(observation_space.name)
