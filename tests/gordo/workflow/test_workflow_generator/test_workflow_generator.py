# -*- coding: utf-8 -*-

import logging
import os
import re

import docker
import pytest
import yaml
from unittest.mock import patch

from click.testing import CliRunner

from gordo.workflow.workflow_generator import workflow_generator as wg
from gordo import cli
from gordo.workflow.config_elements.normalized_config import NormalizedConfig
from gordo.machine.dataset import sensor_tag


logger = logging.getLogger(__name__)


@pytest.fixture(scope="module")
def docker_client():
    """
    Return a docker client
    """
    return docker.from_env()


@pytest.fixture(scope="session")
def path_to_config_files():
    """
    Return the data path containing workflow generator test configuration files
    """
    return os.path.join(os.path.dirname(__file__), "data")


@pytest.fixture(scope="module")
def argo_docker_image(docker_client, repo_dir):
    """
    Build the argo dockerfile
    """
    file = os.path.join(repo_dir, "Dockerfile-GordoDeploy")

    logger.info("Building Argo docker image...")
    img, _ = docker_client.images.build(
        path=repo_dir,
        dockerfile=file,
        tag="temp-argo",
        use_config_proxy=True,
        buildargs={"HTTPS_PROXY": ""},
    )

    yield img

    docker_client.images.remove(img.id, force=True)


def _generate_test_workflow_yaml(
    path_to_config_files, config_filename, project_name="test-proj-name"
):
    """
    Reads a test-config file with workflow_generator, and returns the parsed
    yaml of the generated workflow
    """
    getvalue = _generate_test_workflow_str(
        path_to_config_files, config_filename, project_name=project_name
    )
    expanded_template = yaml.load(getvalue, Loader=yaml.FullLoader)

    return expanded_template


def _generate_test_workflow_str(
    path_to_config_files, config_filename, project_name="test-proj-name"
):
    """
    Reads a test-config file with workflow_generator, and returns the string
    content of the generated workflow
    """
    config_file = os.path.join(path_to_config_files, config_filename)
    args = [
        "workflow",
        "generate",
        "--machine-config",
        config_file,
        "--project-name",
        project_name,
    ]
    runner = CliRunner()

    with patch.object(sensor_tag, "_asset_from_tag_name", return_value="default"):
        result = runner.invoke(cli.gordo, args)

    if result.exception is not None:
        raise result.exception
    return result.output


def _get_env_for_machine_build_serve_task(machine, expanded_template):
    templates = expanded_template["spec"]["templates"]
    do_all = [task for task in templates if task["name"] == "do-all"][0]
    model_builder_machine = [
        task
        for task in do_all["dag"]["tasks"]
        if task["name"] == f"model-builder-{machine}"
    ][0]
    model_builder_machine_env = {
        e["name"]: e["value"] for e in model_builder_machine["arguments"]["parameters"]
    }
    return model_builder_machine_env


@pytest.mark.parametrize(
    "fp_config",
    ("/home/gordo/examples/config_legacy.yaml", "/home/gordo/examples/config_crd.yaml"),
)
@pytest.mark.dockertest
def test_argo_lint(argo_docker_image, fp_config, docker_client, repo_dir):
    """
    Test the example config files, assumed to be valid, produces a valid workflow via `argo lint`
    """

    logger.info("Running workflow generator and argo lint on examples/config.yaml...")
    result = docker_client.containers.run(
        argo_docker_image.id,
        # FIXME: Remove the kubectl stuff from this command
        # We need this kubectl stuff here because argo lint has now started requiring a
        # kubectl config to run. When that is changed we remove the "kubectl" lines
        # below, and the "--username/--password" part of argo lint.
        # Tracking issue:
        # https://github.com/argoproj/argo/issues/1662
        command="bash -c '"
        "kubectl config set-cluster lame-cluster --server=https://lame.org:4443 && "
        "kubectl config set-context lame-context --cluster=lame-cluster &&"
        "kubectl config use-context lame-context &&"
        "gordo "
        "workflow "
        "generate "
        "--project-name some-project "
        f"--machine-config {fp_config} "
        "--output-file /home/gordo/out.yaml "
        "&& argo lint /home/gordo/out.yaml --username lame --password lame'",
        auto_remove=True,
        stderr=True,
        stdout=True,
        detach=False,
    )
    assert result.decode().strip().split("\n")[-1] == "Workflow manifests validated"


def test_basic_generation(path_to_config_files):
    """
    Model must be included in the config file

    start/end dates ...always included? or default to specific dates if not included?
    """

    project_name = "some-fancy-project-name"
    model_config = '{"sklearn.pipeline.Pipeline": {"steps": ["sklearn.preprocessing.data.MinMaxScaler", {"gordo.machine.model.models.KerasAutoEncoder": {"kind": "feedforward_hourglass"}}]}}'

    config_filename = "config-test-with-models.yml"
    expanded_template = _generate_test_workflow_str(
        path_to_config_files, config_filename, project_name=project_name
    )

    assert (
        project_name in expanded_template
    ), f"Expected to find project name: {project_name} in output: {expanded_template}"

    assert (
        model_config in expanded_template
    ), f"Expected to find model config: {model_config} in output: {expanded_template}"

    yaml_content = wg.get_dict_from_yaml(
        os.path.join(path_to_config_files, config_filename)
    )

    with patch.object(sensor_tag, "_asset_from_tag_name", return_value="default"):
        machines = NormalizedConfig(yaml_content, project_name=project_name).machines

    assert len(machines) == 2


def test_generation_to_file(tmpdir, path_to_config_files):
    """
    Test that the workflow generator can output to a file, and it matches
    what would have been output to stdout.
    """
    project_name = "my-sweet-project"
    config_filename = "config-test-with-models.yml"
    expanded_template = _generate_test_workflow_str(
        path_to_config_files, config_filename, project_name=project_name
    )

    # Execute CLI by passing a file to write to.
    config_file = os.path.join(path_to_config_files, config_filename)
    outfile = os.path.join(tmpdir, "out.yml")
    args = [
        "workflow",
        "generate",
        "--machine-config",
        config_file,
        "--project-name",
        project_name,
        "--output-file",
        outfile,
    ]
    runner = CliRunner()
    with patch.object(sensor_tag, "_asset_from_tag_name", return_value="default"):
        result = runner.invoke(cli.gordo, args)
    assert result.exit_code == 0

    # Open the file and ensure they are the same
    with open(outfile, "r") as f:
        outfile_contents = f.read()
    assert outfile_contents.rstrip() == expanded_template.rstrip()


def test_quotes_work(path_to_config_files):
    """Tests that quotes various places result in valid yaml"""
    expanded_template = _generate_test_workflow_yaml(
        path_to_config_files, "config-test-quotes.yml"
    )
    model_builder_machine_1_env = _get_env_for_machine_build_serve_task(
        "machine-1", expanded_template
    )

    machine_1_metadata = yaml.safe_load(model_builder_machine_1_env["machine"])
    assert machine_1_metadata["metadata"]["user_defined"]["machine-metadata"] == {
        "withSingle": "a string with ' in it",
        "withDouble": 'a string with " in it',
        "single'in'key": "why not",
    }

    machine_1_dataset = yaml.safe_load(model_builder_machine_1_env["machine"])
    assert machine_1_dataset["dataset"]["tag_list"] == ["CT/1", 'CT"2', "CT'3"]


def test_overrides_builder_datasource(path_to_config_files):
    expanded_template = _generate_test_workflow_yaml(
        path_to_config_files, "config-test-datasource.yml"
    )

    model_builder_machine_1_env = _get_env_for_machine_build_serve_task(
        "machine-1", expanded_template
    )
    model_builder_machine_2_env = _get_env_for_machine_build_serve_task(
        "machine-2", expanded_template
    )
    model_builder_machine_3_env = _get_env_for_machine_build_serve_task(
        "machine-3", expanded_template
    )

    # ct_23_0002 uses the global overriden requests, but default limits
    assert {"type": "DataLakeProvider", "threads": 20} == yaml.safe_load(
        model_builder_machine_1_env["machine"]
    )["dataset"]["data_provider"]

    # This value must be changed if we change the default values
    assert {"type": "RandomDataProvider", "threads": 15} == yaml.safe_load(
        model_builder_machine_2_env["machine"]
    )["dataset"]["data_provider"]

    # ct_23_0003 uses locally overriden request memory
    assert {"type": "DataLakeProvider", "threads": 10} == yaml.safe_load(
        model_builder_machine_3_env["machine"]
    )["dataset"]["data_provider"]


def test_runtime_overrides_builder(path_to_config_files):
    expanded_template = _generate_test_workflow_yaml(
        path_to_config_files, "config-test-runtime-resource.yaml"
    )
    templates = expanded_template["spec"]["templates"]
    model_builder_task = [
        task for task in templates if task["name"] == "model-builder"
    ][0]
    model_builder_resource = model_builder_task["container"]["resources"]

    # We use yaml overriden memory (both request and limits).
    assert model_builder_resource["requests"]["memory"] == "121M"

    # This was specified to 120 in the config file, but is bumped to match the
    # request
    assert model_builder_resource["limits"]["memory"] == "121M"
    # requests.cpu is all default
    assert model_builder_resource["requests"]["cpu"] == "1001m"


def test_runtime_overrides_client_para(path_to_config_files):
    """
    It is possible to override the parallelization of the client
    through the globals-section of the config file
    """
    expanded_template = _generate_test_workflow_yaml(
        path_to_config_files, "config-test-runtime-resource.yaml"
    )
    templates = expanded_template["spec"]["templates"]
    client_task = [task for task in templates if task["name"] == "gordo-client-waiter"][
        0
    ]

    client_env = {e["name"]: e["value"] for e in client_task["script"]["env"]}

    assert client_env["GORDO_MAX_CLIENTS"] == "10"


def test_runtime_overrides_client(path_to_config_files):
    expanded_template = _generate_test_workflow_yaml(
        path_to_config_files, "config-test-runtime-resource.yaml"
    )
    templates = expanded_template["spec"]["templates"]
    model_client_task = [task for task in templates if task["name"] == "gordo-client"][
        0
    ]
    model_client_resource = model_client_task["script"]["resources"]

    # We use yaml overriden memory (both request and limits).
    assert model_client_resource["requests"]["memory"] == "221M"

    # This was specified to 120 in the config file, but is bumped to match the
    # request
    assert model_client_resource["limits"]["memory"] == "221M"
    # requests.cpu is all default
    assert model_client_resource["requests"]["cpu"] == "100m"


def test_runtime_overrides_influx(path_to_config_files):
    expanded_template = _generate_test_workflow_yaml(
        path_to_config_files, "config-test-runtime-resource.yaml"
    )
    templates = expanded_template["spec"]["templates"]
    influx_task = [
        task for task in templates if task["name"] == "gordo-influx-statefulset"
    ][0]
    influx_statefulset_definition = yaml.load(
        influx_task["resource"]["manifest"], Loader=yaml.FullLoader
    )
    influx_resource = influx_statefulset_definition["spec"]["template"]["spec"][
        "containers"
    ][0]["resources"]
    # We use yaml overriden memory (both request and limits).
    assert influx_resource["requests"]["memory"] == "321M"

    # This was specified to 120 in the config file, but is bumped to match the
    # request
    assert influx_resource["limits"]["memory"] == "321M"
    # requests.cpu is default
    assert influx_resource["requests"]["cpu"] == "520m"
    assert influx_resource["limits"]["cpu"] == "10040m"


def test_disable_influx(path_to_config_files):
    """
    It works to disable influx globally
    """
    expanded_template = _generate_test_workflow_yaml(
        path_to_config_files, "config-test-disable-influx.yml"
    )
    templates = expanded_template["spec"]["templates"]
    do_all = [task for task in templates if task["name"] == "do-all"][0]
    influx_tasks = [
        task["name"] for task in do_all["dag"]["tasks"] if "influx" in task["name"]
    ]
    client_tasks = [
        task["name"] for task in do_all["dag"]["tasks"] if "client" in task["name"]
    ]

    # The cleanup should be the only influx-related task
    assert influx_tasks == ["influx-cleanup"]
    assert client_tasks == []


def test_selective_influx(path_to_config_files):
    """
    It works to enable/disable influx per machine
    """
    expanded_template = _generate_test_workflow_yaml(
        path_to_config_files, "config-test-selective-influx.yml"
    )
    templates = expanded_template["spec"]["templates"]
    do_all = [task for task in templates if task["name"] == "do-all"][0]
    influx_tasks = [
        task["name"] for task in do_all["dag"]["tasks"] if "influx" in task["name"]
    ]
    client_tasks = [
        task["name"] for task in do_all["dag"]["tasks"] if "client" in task["name"]
    ]

    # Now we should have both influx and influx-cleanup
    assert influx_tasks == ["influx-cleanup", "gordo-influx"]

    # And we have a single client task for the one client we want running
    assert client_tasks == ["gordo-client-ct-23-0002"]


@pytest.mark.parametrize("output_to_file", (True, False))
def test_main_tag_list(output_to_file, path_to_config_files, tmpdir):
    config_file = os.path.join(path_to_config_files, "config-test-tag-list.yml")
    args = ["workflow", "unique-tags", "--machine-config", config_file]

    out_file = os.path.join(tmpdir, "out.txt")

    if output_to_file:
        args.extend(["--output-file-tag-list", out_file])

    runner = CliRunner()
    with patch.object(sensor_tag, "_asset_from_tag_name", return_value="default"):
        result = runner.invoke(cli.gordo, args)

    assert result.exit_code == 0

    if output_to_file:
        assert os.path.isfile(out_file)
    else:
        output_tags = set(result.output.split(sep="\n")[:-1])
        expected_output_tags = {"Tag 1", "Tag 2", "Tag 3", "Tag 4", "Tag 5"}

        assert (
            output_tags == expected_output_tags
        ), f"Expected to find: {expected_output_tags}, outputted {output_tags}"


def test_valid_dateformats(path_to_config_files):
    output_workflow = _generate_test_workflow_str(
        path_to_config_files, "config-test-allowed-timestamps.yml"
    )
    # Three from the dataset, three from the start for tag fetching, and three in
    # each machine's model-crd specification
    assert output_workflow.count("2016-11-07") == 9
    assert output_workflow.count("2017-11-07") == 6


def test_model_names_embedded(path_to_config_files):
    """
    Tests that the generated workflow contains the names of the machines
    it builds a workflow for in the metadata/annotation as a yaml-parsable structure
    """
    output_workflow = _generate_test_workflow_yaml(
        path_to_config_files, "config-test-allowed-timestamps.yml"
    )
    parsed_machines = yaml.load(
        output_workflow["metadata"]["annotations"]["gordo-models"],
        Loader=yaml.FullLoader,
    )
    assert parsed_machines == ["machine-1", "machine-2", "machine-3"]


def test_missing_timezone(path_to_config_files):
    with pytest.raises(ValueError):
        _generate_test_workflow_yaml(
            path_to_config_files, "config-test-missing-timezone.yml"
        )

    with pytest.raises(ValueError):
        _generate_test_workflow_yaml(
            path_to_config_files, "config-test-missing-timezone-quoted.yml"
        )


def test_validates_resource_format(path_to_config_files):
    """
    We validate that resources are integers
    """
    with pytest.raises(ValueError):
        _generate_test_workflow_str(
            path_to_config_files, "config-test-failing-resource-format.yml"
        )


@pytest.mark.parametrize(
    "owner_ref_str,valid",
    (
        ("[]", False),
        ("- key: value", False),
        (
            """
            - uid: 1
              name: name
              kind: kind
              apiVersion: v1
            """,
            True,
        ),
    ),
)
def test_valid_owner_ref(owner_ref_str: str, valid: bool):
    if valid:
        wg._valid_owner_ref(owner_ref_str)
    else:
        with pytest.raises(TypeError):
            wg._valid_owner_ref(owner_ref_str)


@pytest.mark.parametrize(
    "test_file, log_level",
    (
        ("config-test-with-log-key.yml", "DEBUG"),
        ("config-test-with-models.yml", "INFO"),
    ),
)
def test_log_level_key(test_file: str, log_level: str, path_to_config_files: str):
    """
    Test that GORDO_LOG_LEVEL is set to the correct value if specified in the config file, or default to INFO if not
    specified.
    """
    workflow_str = _generate_test_workflow_str(path_to_config_files, test_file)

    # Find the value on the next line after the key GORDO_LOG_LEVEL
    gordo_log_levels = re.findall(
        r"(?<=GORDO_LOG_LEVEL\r|GORDO_LOG_LEVEL\n)[^\r\n]+", workflow_str
    )

    # Assert all the values to the GORDO_LOG_LEVEL key contains the correct log-level
    assert all([log_level in value for value in gordo_log_levels])
