"""Load bounded artifact/test verification without granting task admission."""
from pathlib import Path

from .automatic_verification import AutomaticVerifier
from .execution_runtime import _path


def check_evidence_location(directory, projects):
    directory = _path(str(directory), directory=True)
    for project in projects.values():
        workspace = Path(project['workspace']).resolve(strict=True)
        if directory.is_relative_to(workspace) or workspace.is_relative_to(directory):
            raise ValueError('Verification evidence must be separate from project workspaces')


def load_verifier(config, *, tasks, projects):
    if not isinstance(config, dict) or not {'evidenceDirectory'} <= set(config) <= {'evidenceDirectory', 'testExecutor'}:
        raise ValueError('Exact verification deployment configuration required')
    directory = _path(config['evidenceDirectory'], directory=True)
    check_evidence_location(directory, projects)
    executor = None
    if 'testExecutor' in config:
        from .test_executor import DockerTestExecutor
        settings = config['testExecutor']
        if not isinstance(settings, dict) or set(settings) != {'imageId'}:
            raise ValueError('Exact isolated test executor configuration required')
        executor = DockerTestExecutor(directory, settings['imageId'])
    return AutomaticVerifier(tasks, directory, test_executor=executor)
