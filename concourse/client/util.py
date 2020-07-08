# Copyright (c) 2019-2020 SAP SE or an SAP affiliate company. All rights reserved. This file is
# licensed under the Apache Software License, v. 2 except as noted otherwise in the LICENSE file
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import dataclasses
import json
import time
import typing

import dateutil.parser

from dacite import from_dict
from dacite.exceptions import DaciteError
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import (
    datetime,
    timedelta,
)

from ci.util import (
    info,
    warning,
)
from whd.model import (
    PullRequestEvent,
)
from .model import (
    Job,
    PipelineConfigResource,
    PipelineResource,
    ResourceType,
    ResourceVersion,
)


class PinningFailedError(Exception):
    pass


class PinningUnnecessaryError(Exception):
    pass


@dataclass
class PinComment:
    pin_timestamp: str
    version: dict
    next_retry: str
    comment: str


def determine_pr_resource_versions(
    pr_event: PullRequestEvent,
    concourse_api
) -> typing.Iterator[(PipelineConfigResource, typing.Sequence[ResourceVersion])]:

    pr_resources = concourse_api.pipeline_resources(
        pipeline_names=concourse_api.pipelines(),
        resource_type=ResourceType.PULL_REQUEST,
    )

    for pr_resource in pr_resources:
        ghs = pr_resource.github_source()
        pr_repository = pr_event.repository()
        if not ghs.hostname() == pr_repository.github_host():
            continue
        if not ghs.repo_path().lstrip('/') == pr_repository.repository_path():
            continue

        pipeline_name = pr_resource.pipeline.name
        pr_resource_versions = concourse_api.resource_versions(
            pipeline_name=pipeline_name,
            resource_name=pr_resource.name,
        )

        # only interested in pr resource versions, which match the PR number
        pr_resource_versions = [
            rv for rv in pr_resource_versions if rv.version()['pr'] == str(pr_event.number())
        ]

        yield (pr_resource, pr_resource_versions)


def determine_jobs_to_be_triggered(
    resource: PipelineConfigResource,
) -> typing.Iterator[Job]:

    yield from (
        job for job in resource.pipeline.jobs if job.is_triggered_by_resource(resource.name)
    )


def wait_for_job_to_be_triggered(
    job: Job,
    resource_version: ResourceVersion,
    concourse_api,
    retries: int=10,
    sleep_time_seconds: int=5,
) -> bool:
    '''
    There is some delay between the update of a resource and the start of the corresponding job.
    Therefore we have to wait some time to decide if the job has been triggered or not.

    @return True if the job has been triggered
    '''
    while retries >= 0:
        if has_job_been_triggered(
            job=job,
            resource_version=resource_version,
            concourse_api=concourse_api,
        ):
            return True

        time.sleep(sleep_time_seconds)
        retries -= 1

    return False


def has_job_been_triggered(
    job: Job,
    resource_version: ResourceVersion,
    concourse_api,
) -> bool:

    builds = concourse_api.job_builds(job.pipeline.name, job.name)
    for build in builds:
        build_plan = build.plan()
        if build_plan.contains_version_ref(resource_version.version()['ref']):
            return True
    return False


def jobs_not_triggered(
    pr_event: PullRequestEvent,
    concourse_api,
) -> typing.Iterator[(Job, PipelineConfigResource, ResourceVersion)]:

    for pr_resource, pr_resource_versions in determine_pr_resource_versions(
        pr_event=pr_event,
        concourse_api=concourse_api,
    ):
        for job in determine_jobs_to_be_triggered(pr_resource):
            for pr_resource_version in pr_resource_versions:
                if not wait_for_job_to_be_triggered(job, pr_resource_version, concourse_api):
                    yield (job, pr_resource, pr_resource_version)


def acquire_resource_lock_and_trigger_build(
    job: Job,
    resource: PipelineConfigResource,
    resource_version: ResourceVersion,
    concourse_api,
    max_retries: int=3,
):
    retries = max_retries
    try:
        with pin_resource(
            resource=resource,
            resource_version=resource_version,
            concourse_api=concourse_api,
        ):
            # setup initial pin comment with next retry time
            job_retries = 10
            sleep_seconds_between_attempts = 5
            now = datetime.now()
            next_retry = now + timedelta(
                seconds=job_retries * sleep_seconds_between_attempts,
            )
            pin_comment = PinComment(
                pin_timestamp=now.isoformat(),
                version_data=resource_version.version(),
                next_retry=next_retry.isoformat(),
                comment=(
                    "pinned by technical user 'concourse', please do not unpin before "
                    f"{next_retry.isoformat()}"
                ),

            while retries >= 0:
                concourse_api.trigger_build(
                    pipeline_name=resource.pipeline_name(),
                    job_name=job.pipeline_name,
                )

                if wait_for_job_to_be_triggered(
                    job=job,
                    resource_version=resource_version,
                    concourse_api=concourse_api,
                    retries=job_retries,
                    sleep_time_seconds=sleep_seconds_between_attempts,
                ):
                    info(
                        f'job {job.name} for resource version {resource_version.version()} '
                        'has been triggered'
                    )
                    return
                else:
                    now = datetime.now()
                    next_retry = now + timedelta(
                        seconds=job_retries * sleep_seconds_between_attempts,
                    )
                    pin_comment = PinComment(
                        pin_timestamp=now.isoformat(),
                        version_data=resource_version.version(),
                        next_retry=next_retry.isoformat(),
                        comment=(
                            "pinned by technical user 'concourse', please do not unpin before "
                            f"{next_retry.isoformat()}"
                        ),
                    )
                    concourse_api.pin_comment(
                        pipeline_name=resource.pipeline_name(),
                        resource_name=resource.name,
                        comment=json.dumps(dataclasses.asdict(pin_comment)),
                    )
                    warning(
                        f'job {job.name} for resource version {resource_version.version()} '
                        f'could not be triggered. Will retry {retries} more time(s)'
                    )

            warning(
                f'job {job.name} for resource version {resource_version.version()} '
                f'could not be triggered. Giving up after {max_retries} retries'
            )


@contextmanager
def pin_resource(
    resource: PipelineResource,
    resource_version: ResourceVersion,
    concourse_api,
):
    try:
        _ensure_resource_unpinned(
            resource=resource,
            resource_version=resource_version,
            concourse_api=concourse_api,
        )
        yield
    finally:
        concourse_api.unpin_resource(
            pipeline_name=resource.pipeline_name(),
            resource_name=resource.name,
        )


def _ensure_resource_unpinned(
    resource: PipelineResource,
    resource_version: ResourceVersion,
    concourse_api,
):
    previous_retry_time = None
    start_time = datetime.now()
    while True:
        cc_resource = concourse_api.resource(
            pipeline_name=resource.pipeline_name(),
            resource_name=resource.name,
        )

        if not cc_resource.is_pinned():
            return

        try:
            pin_comment = from_dict(
                data_class=PinComment,
                data=json.loads(cc_resource.pin_comment()),
            )
            if pin_comment.version == resource_version.version():
                raise PinningUnnecessaryError("Resource is already pinned for the same version")

            next_retry_time = dateutil.parser.isoparse(pin_comment.next_retry)
            if previous_retry_time and next_retry_time == previous_retry_time:
                warning(
                    f'found resource which was not unpinned '
                    f'{resource.pipeline_name()=} {resource.name=}. Unpinning resource now'
                )
                concourse_api.unpin_resource(
                    pipeline_name=resource.pipeline_name(),
                    resource_name=resource.name,
                )
                return
        except json.JSONDecodeError:
            info(
                f'Resource comment for {resource.name=} of {resource.pipeline_name()=} '
                'could not be parsed - most likely pinned by human user'
            )
        except DaciteError:
            info(
                f'Resource comment for {resource.name=} of {resource.pipeline_name()=} '
                'could not be instantiated'
            )

        previous_retry_time = next_retry_time
        now = datetime.now()
        if (start_time - now).minutes > 5:
            raise PinningFailedError(
                f'Tried at least 5 minutes to unpin '
                f'{resource.name=} of pipeline {resource.pipeline_name()=}'
            )

        time.sleep((next_retry_time - now).seconds + 5)
