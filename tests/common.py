# Copyright 2022 NVIDIA Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os

def prepend_path(l):
    return [os.path.join("testdata", f) for f in l]

test_mtx_files = prepend_path([
    "test.mtx",
    "GlossGT.mtx",
    "Ragusa18.mtx",
    "cage4.mtx",
    "karate.mtx",
])

