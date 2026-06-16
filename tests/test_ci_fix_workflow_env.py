"""Tests for deterministic job-environment classification.

Code owns environment selection: parse runs-on and container.image only, and
refuse anything it does not clearly understand.
"""

from __future__ import annotations

from scripts.ci_fix.verify.base import VerifyEnv
from scripts.ci_fix.verify.workflow_env import classify_job_environment

_WF = """
jobs:
  test-ubuntu-latest:
    runs-on: ubuntu-latest
    steps:
      - run: make test
  build-almalinux8:
    runs-on: ubuntu-latest
    container: almalinux:8
    steps:
      - run: make
  build-debian:
    runs-on: ubuntu-latest
    container:
      image: debian:bullseye
    steps:
      - run: make
  build-macos-latest:
    runs-on: macos-latest
    steps:
      - run: make
  bench:
    runs-on: ["self-hosted", "arm64"]
    steps:
      - run: make
  matrix-runner:
    runs-on: ${{ matrix.os }}
    steps:
      - run: make
  dynamic-image:
    runs-on: ubuntu-latest
    container: ${{ matrix.container }}
    steps:
      - run: make
"""


def test_plain_ubuntu_is_local():
    env = classify_job_environment(_WF, "test-ubuntu-latest")
    assert env.env is VerifyEnv.LOCAL
    assert env.image == ""


def test_container_string_is_docker():
    env = classify_job_environment(_WF, "build-almalinux8")
    assert env.env is VerifyEnv.DOCKER
    assert env.image == "almalinux:8"


def test_container_mapping_image_is_docker():
    env = classify_job_environment(_WF, "build-debian")
    assert env.env is VerifyEnv.DOCKER
    assert env.image == "debian:bullseye"


def test_macos_runner():
    assert classify_job_environment(_WF, "build-macos-latest").env is VerifyEnv.MACOS


def test_self_hosted_list_is_unsupported():
    env = classify_job_environment(_WF, "bench")
    assert env.env is VerifyEnv.UNSUPPORTED
    assert "unsupported runner" in env.reason


def test_matrix_runner_is_unsupported():
    assert classify_job_environment(_WF, "matrix-runner").env is VerifyEnv.UNSUPPORTED


def test_dynamic_container_image_is_unsupported():
    env = classify_job_environment(_WF, "dynamic-image")
    assert env.env is VerifyEnv.UNSUPPORTED
    assert "dynamic or malformed" in env.reason


def test_missing_job_is_unsupported():
    env = classify_job_environment(_WF, "no-such-job")
    assert env.env is VerifyEnv.UNSUPPORTED
    assert "not found" in env.reason


def test_malformed_yaml_is_unsupported():
    env = classify_job_environment("jobs: [this is: not valid", "x")
    assert env.env is VerifyEnv.UNSUPPORTED


_ARM_WF = """
jobs:
  test-arm:
    runs-on: ubuntu-24.04-arm
    steps:
      - run: make
  test-x86:
    runs-on: ubuntu-24.04
    steps:
      - run: make
"""


def test_arm_runner_is_unsupported():
    # ubuntu-*-arm must NOT be classified local/x86.
    env = classify_job_environment(_ARM_WF, "test-arm")
    assert env.env is VerifyEnv.UNSUPPORTED


def test_x86_ubuntu_version_label_is_local():
    assert classify_job_environment(_ARM_WF, "test-x86").env is VerifyEnv.LOCAL


_REGISTRY_WF = """
jobs:
  port-image:
    runs-on: ubuntu-latest
    container: ghcr.io:443/org/image:tag
    steps:
      - run: make
  digest-image:
    runs-on: ubuntu-latest
    container:
      image: ghcr.io/org/image@sha256:""" + ("a" * 64) + """
    steps:
      - run: make
"""


def test_registry_port_and_digest_images_classify_docker():
    assert classify_job_environment(_REGISTRY_WF, "port-image").env is VerifyEnv.DOCKER
    assert classify_job_environment(_REGISTRY_WF, "digest-image").env is VerifyEnv.DOCKER
