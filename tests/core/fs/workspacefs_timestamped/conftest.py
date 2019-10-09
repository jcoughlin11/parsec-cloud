# Parsec Cloud (https://parsec.cloud) Copyright (c) AGPLv3 2019 Scille SAS

import pytest
import pendulum

from tests.common import freeze_time

day0 = pendulum.Pendulum(1999, 12, 31)
day1 = pendulum.Pendulum(2000, 1, 1)
day2 = pendulum.Pendulum(2000, 1, 2)
day3 = pendulum.Pendulum(2000, 1, 3)
day4 = pendulum.Pendulum(2000, 1, 4)
day5 = pendulum.Pendulum(2000, 1, 5)


@pytest.fixture
@pytest.mark.trio
async def alice_workspace(alice_user_fs, running_backend):
    with freeze_time(day0):
        wid = await alice_user_fs.workspace_create("w")
        workspace = alice_user_fs.get_workspace(wid)
        await workspace.mkdir("/foo")
        # No sync, we want alice_workspace.to_timestamped to fail at day0
    with freeze_time(day1):
        await workspace.sync()
    with freeze_time(day2):
        await workspace.touch("/foo/bar")
        await workspace.sync()
    with freeze_time(day3):
        await workspace.touch("/foo/baz")
        await workspace.sync()

    with freeze_time(day4):
        await workspace.mkdir("/files")
        await workspace.touch("/files/content")
        await workspace.write_bytes("/files/content", b"abcde")
        await workspace.sync()
    with freeze_time(day5):
        await workspace.write_bytes("/files/content", b"fghij")
        await workspace.sync()

    return workspace


@pytest.fixture
async def timestamp_0():
    return day0


@pytest.fixture
async def alice_workspace_t1(alice_workspace):
    return await alice_workspace.to_timestamped(day1)


@pytest.fixture
async def alice_workspace_t2(alice_workspace_t1):
    return await alice_workspace_t1.to_timestamped(day2)


@pytest.fixture
async def alice_workspace_t3(alice_workspace_t2):
    return await alice_workspace_t2.to_timestamped(day3)


@pytest.fixture
async def alice_workspace_t4(alice_workspace_t3):
    return await alice_workspace_t3.to_timestamped(day4)


@pytest.fixture
async def alice_workspace_t5(alice_workspace_t4):
    return await alice_workspace_t4.to_timestamped(day5)
