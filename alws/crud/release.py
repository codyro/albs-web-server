import asyncio
import re
import typing
from collections import defaultdict

import jmespath
from sqlalchemy.future import select
from sqlalchemy.orm import Session, selectinload

from alws import models
from alws.config import settings
from alws.constants import ReleaseStatus, RepoType, PackageNevra
from alws.errors import (DataNotFoundError, EmptyReleasePlan,
                         MissingRepository, SignError)
from alws.schemas import release_schema
from alws.utils.beholder_client import BeholderClient
from alws.utils.debuginfo import is_debuginfo_rpm
from alws.utils.pulp_client import PulpClient
from alws.crud import sign_task


async def __get_pulp_packages(
        db: Session, build_ids: typing.List[int],
        build_tasks: typing.List[int] = None) \
        -> typing.Tuple[typing.List[dict], typing.List[str]]:
    src_rpm_names = []
    packages_fields = ['name', 'epoch', 'version', 'release', 'arch']
    pulp_packages = []
    pulp_client = PulpClient(
        settings.pulp_host,
        settings.pulp_user,
        settings.pulp_password
    )

    builds_q = select(models.Build).where(
        models.Build.id.in_(build_ids)).options(
        selectinload(
            models.Build.source_rpms).selectinload(
            models.SourceRpm.artifact),
        selectinload(
            models.Build.binary_rpms).selectinload(
            models.BinaryRpm.artifact)
    )
    build_result = await db.execute(builds_q)
    for build in build_result.scalars().all():
        for src_rpm in build.source_rpms:
            # Failsafe to not process logs
            if src_rpm.artifact.type != 'rpm':
                continue
            if build_tasks \
                    and src_rpm.artifact.build_task_id not in build_tasks:
                continue
            src_rpm_names.append(src_rpm.artifact.name)
            pkg_info = await pulp_client.get_rpm_package(
                src_rpm.artifact.href, include_fields=packages_fields)
            pkg_info['artifact_href'] = src_rpm.artifact.href
            pkg_info['full_name'] = src_rpm.artifact.name
            pulp_packages.append(pkg_info)
        for binary_rpm in build.binary_rpms:
            # Failsafe to not process logs
            if binary_rpm.artifact.type != 'rpm':
                continue
            if build_tasks \
                    and binary_rpm.artifact.build_task_id not in build_tasks:
                continue
            pkg_info = await pulp_client.get_rpm_package(
                binary_rpm.artifact.href, include_fields=packages_fields)
            pkg_info['artifact_href'] = binary_rpm.artifact.href
            pkg_info['full_name'] = binary_rpm.artifact.name
            pulp_packages.append(pkg_info)
    return pulp_packages, src_rpm_names


async def get_release_plan(db: Session, build_ids: typing.List[int],
                           base_dist_version: str,
                           reference_dist_name: str,
                           reference_dist_version: str,
                           build_tasks: typing.List[int] = None) -> dict:
    clean_ref_dist_name = re.search(
        r'(?P<dist_name>[a-z]+)', reference_dist_name,
        re.IGNORECASE).groupdict().get('dist_name')
    clean_ref_dist_name_lower = clean_ref_dist_name.lower()
    endpoint = f'/api/v1/distros/{clean_ref_dist_name}/' \
               f'{reference_dist_version}/projects/'
    packages = []
    repo_name_regex = re.compile(r'\w+-\d-(?P<name>\w+(-\w+)?)')
    pulp_client = PulpClient(
        settings.pulp_host,
        settings.pulp_user,
        settings.pulp_password
    )
    pulp_packages, src_rpm_names = await __get_pulp_packages(
        db, build_ids, build_tasks=build_tasks)

    async def check_package_presence_in_repo(pkgs_nevra: dict,
                                             repo_ver_href: str):
        params = {
            'name__in': ','.join(pkgs_nevra['name']),
            'epoch__in': ','.join(pkgs_nevra['epoch']),
            'version__in': ','.join(pkgs_nevra['version']),
            'release__in': ','.join(pkgs_nevra['release']),
            'arch': 'noarch',
            'repository_version': repo_ver_href,
            'fields': 'name,epoch,version,release,arch',
        }
        packages = await pulp_client.get_rpm_packages(params)
        if packages:
            repo_href = re.sub(r'versions\/\d+\/$', '', repo_ver_href)
            pkg_fullnames = [
                pkgs_mapping.get(PackageNevra(
                    pkg['name'], pkg['epoch'], pkg['version'],
                    pkg['release'], pkg['arch']
                ))
                for pkg in packages
            ]
            for fullname in filter(None, pkg_fullnames):
                existing_packages[fullname].append(
                    repo_ids_by_href.get(repo_href, 0))

    async def prepare_and_execute_async_tasks() -> None:
        tasks = []
        for value in (True, False):
            pkg_dict = debug_pkgs_nevra if value else pkgs_nevra
            if not pkg_dict:
                continue
            for key in ('name', 'epoch', 'version', 'release'):
                pkg_dict[key] = set(pkg_dict[key])
            tasks.extend((
                check_package_presence_in_repo(pkg_dict, repo_href)
                for repo_href, repo_is_debug in latest_prod_repo_versions
                if repo_is_debug is value
            ))
        await asyncio.gather(*tasks)

    def prepare_data_for_executing_async_tasks(package: dict,
                                               full_name: str) -> None:
        pkg_name, pkg_epoch, pkg_version, pkg_release, pkg_arch = (
            package['name'], package['epoch'], package['version'],
            package['release'], package['arch']
        )
        nevra = PackageNevra(pkg_name, pkg_epoch, pkg_version,
                             pkg_release, pkg_arch)
        pkgs_mapping[nevra] = full_name
        if is_debuginfo_rpm(pkg_name):
            debug_pkgs_nevra['name'].append(pkg_name)
            debug_pkgs_nevra['epoch'].append(pkg_epoch)
            debug_pkgs_nevra['version'].append(pkg_version)
            debug_pkgs_nevra['release'].append(pkg_release)
        else:
            pkgs_nevra['name'].append(pkg_name)
            pkgs_nevra['epoch'].append(pkg_epoch)
            pkgs_nevra['version'].append(pkg_version)
            pkgs_nevra['release'].append(pkg_release)

    async def get_pulp_based_response():
        plan_packages = []
        for pkg in pulp_packages:
            full_name = pkg['full_name']
            if full_name in added_packages:
                continue
            if pkg['arch'] == 'noarch':
                prepare_data_for_executing_async_tasks(pkg, full_name)
            plan_packages.append({'package': pkg, 'repositories': []})
            added_packages.append(full_name)
        await prepare_and_execute_async_tasks()

        return {
            'packages': plan_packages,
            'repositories': prod_repos,
            'existing_packages': existing_packages,
        }

    repo_q = select(models.Repository).where(
        models.Repository.production.is_(True))
    result = await db.execute(repo_q)
    prod_repos = []
    tasks = []
    repo_ids_by_href = {}
    for repo in result.scalars().all():
        prod_repos.append({
            'id': repo.id,
            'name': repo.name,
            'arch': repo.arch,
            'debug': repo.debug,
            'url': repo.url,
        })
        tasks.append(pulp_client.get_repo_latest_version(repo.pulp_href,
                                                         for_releases=True))
        repo_ids_by_href[repo.pulp_href] = repo.id
    latest_prod_repo_versions = await asyncio.gather(*tasks)

    repos_mapping = {RepoType(repo['name'], repo['arch'], repo['debug']): repo
                     for repo in prod_repos}
    pkgs_mapping = {}
    added_packages = []
    pkgs_nevra, debug_pkgs_nevra, existing_packages = (
        defaultdict(list), defaultdict(list), defaultdict(list)
    )

    if not settings.package_beholder_enabled:
        return await get_pulp_based_response()

    beholder_response = await BeholderClient(settings.beholder_host).post(
        endpoint, src_rpm_names)
    if not beholder_response.get('packages'):
        return await get_pulp_based_response()
    if beholder_response.get('packages', []):
        for package in pulp_packages:
            pkg_name = package['name']
            pkg_version = package['version']
            pkg_arch = package['arch']
            full_name = package['full_name']
            if full_name in added_packages:
                continue
            if pkg_arch == 'noarch':
                prepare_data_for_executing_async_tasks(package, full_name)
            query = f'packages[].packages[?name==\'{pkg_name}\' ' \
                    f'&& version==\'{pkg_version}\' ' \
                    f'&& arch==\'{pkg_arch}\'][]'
            predicted_package = jmespath.search(query, beholder_response)
            pkg_info = {'package': package, 'repositories': []}
            if predicted_package:
                # JMESPath will find a list with 1 element inside
                predicted_package = predicted_package[0]
                repositories = predicted_package['repositories']
                release_repositories = set()
                for repo in repositories:
                    ref_repo_name = repo['name']
                    repo_name = (repo_name_regex.search(ref_repo_name)
                                 .groupdict()['name'])
                    release_repo_name = (f'{clean_ref_dist_name_lower}'
                                         f'-{base_dist_version}-{repo_name}')
                    debug = ref_repo_name.endswith('debuginfo')
                    if repo['arch'] == 'src':
                        debug = False
                    release_repo = RepoType(
                        release_repo_name, repo['arch'], debug)
                    release_repositories.add(release_repo)
                pkg_info['repositories'] = [
                    repos_mapping.get(item) for item in release_repositories]
            packages.append(pkg_info)
            added_packages.append(full_name)

        # if noarch package already in repo with same NEVRA,
        # we should exclude this repo when generate release plan
        await prepare_and_execute_async_tasks()
        for pkg_info in packages:
            package = pkg_info['package']
            if package['arch'] != 'noarch':
                continue
            repos_ids = existing_packages.get(package['full_name'], [])
            new_repos = [
                repo for repo in pkg_info['repositories']
                if repo['id'] not in repos_ids
            ]
            pkg_info['repositories'] = new_repos
    return {
        'packages': packages,
        'repositories': prod_repos,
        'existing_packages': existing_packages,
    }


async def execute_release_plan(release_id: int, db: Session):
    packages_to_repo_layout = {}

    async with db.begin():
        release_result = await db.execute(
            select(models.Release).where(
                models.Release.id == release_id).options(
                    selectinload(models.Release.platform)))
        release = release_result.scalars().first()
        if not release.plan.get('packages') or \
                not release.plan.get('repositories'):
            raise EmptyReleasePlan('Cannot execute plan with empty packages '
                                   'or repositories: {packages}, {repositories}'
                                   .format_map(release.plan))
    for build_id in release.build_ids:
        try:
            verified = await sign_task.verify_signed_build(
                db, build_id, release.platform.id)
        except (DataNotFoundError, ValueError, SignError) as e:
            msg = f'The build {build_id} was not verified, because\n{e}'
            raise SignError(msg)
        if not verified:
            msg = f'Cannot execute plan with wrong singing of {build_id}'
            raise SignError(msg)
    for package in release.plan['packages']:
        for repository in package['repositories']:
            repo_name = repository['name']
            repo_arch = repository['arch']
            if repo_name not in packages_to_repo_layout:
                packages_to_repo_layout[repo_name] = {}
            if repo_arch not in packages_to_repo_layout[repo_name]:
                packages_to_repo_layout[repo_name][repo_arch] = []
            packages_to_repo_layout[repo_name][repo_arch].append(
                package['package']['artifact_href'])

    pulp_client = PulpClient(
        settings.pulp_host,
        settings.pulp_user,
        settings.pulp_password
    )
    repo_status = {}

    for repository_name, arches in packages_to_repo_layout.items():
        repo_status[repository_name] = {}
        for arch, packages in arches.items():
            repo_q = select(models.Repository).where(
                models.Repository.name == repository_name,
                models.Repository.arch == arch)
            repo_result = await db.execute(repo_q)
            repo = repo_result.scalars().first()
            if not repo:
                raise MissingRepository(
                    f'Repository with name {repository_name} is missing '
                    f'or doesn\'t have pulp_href field')
            result = await pulp_client.modify_repository(
                repo.pulp_href, add=packages)
            # after modify repos we need to publish repo content
            await pulp_client.create_rpm_publication(repo.pulp_href)
            repo_status[repository_name][arch] = result

    return repo_status


async def get_releases(db: Session) -> typing.List[models.Release]:
    release_result = await db.execute(select(models.Release).options(
        selectinload(models.Release.created_by),
        selectinload(models.Release.platform)))
    return release_result.scalars().all()


async def create_new_release(
            db: Session, user_id: int, payload: release_schema.ReleaseCreate
        ) -> models.Release:
    async with db.begin():
        user_q = select(models.User).where(models.User.id == user_id)
        user_result = await db.execute(user_q)
        platform_result = await db.execute(select(models.Platform).where(
            models.Platform.id.in_(
                (payload.platform_id, payload.reference_platform_id))))
        platforms = platform_result.scalars().all()
        base_platform = [item for item in platforms
                         if item.id == payload.platform_id][0]
        reference_platform = [item for item in platforms
                              if item.id == payload.reference_platform_id][0]

        user = user_result.scalars().first()
        new_release = models.Release()
        new_release.build_ids = payload.builds
        if getattr(payload, 'build_tasks', None):
            new_release.build_task_ids = payload.build_tasks
        new_release.platform = base_platform
        new_release.reference_platform_id = payload.reference_platform_id
        new_release.plan = await get_release_plan(
            db, payload.builds,
            base_platform.distr_version,
            reference_platform.name,
            reference_platform.distr_version,
            build_tasks=payload.build_tasks
        )
        new_release.created_by = user
        db.add(new_release)
        await db.commit()

    await db.refresh(new_release)
    release_res = await db.execute(select(models.Release).where(
        models.Release.id == new_release.id).options(
        selectinload(models.Release.created_by),
        selectinload(models.Release.platform)
    ))
    return release_res.scalars().first()


async def update_release(
        db: Session, release_id: int,
        payload: release_schema.ReleaseUpdate
) -> models.Release:
    async with db.begin():
        release_result = await db.execute(select(models.Release).where(
            models.Release.id == release_id).with_for_update())
        release = release_result.scalars().first()
        if not release:
            raise DataNotFoundError(f'Release with ID {release_id} not found')
        if payload.plan:
            release.plan = payload.plan
        build_tasks = getattr(payload, 'build_tasks', None)
        if (payload.builds and payload.builds != release.build_ids) or \
                (build_tasks and build_tasks != release.build_task_ids):
            release.build_ids = payload.builds
            if build_tasks:
                release.build_task_ids = payload.build_tasks
            platform_result = await db.execute(select(models.Platform).where(
                models.Platform.id.in_(
                    (release.platform_id, release.reference_platform_id))))
            platforms = platform_result.scalars().all()
            base_platform = [item for item in platforms
                             if item.id == release.platform_id][0]
            reference_platform = [
                item for item in platforms
                if item.id == release.reference_platform_id][0]
            release.plan = await get_release_plan(
                db, payload.builds,
                base_platform.distr_version,
                reference_platform.name,
                reference_platform.distr_version,
                build_tasks=payload.build_tasks
            )
        db.add(release)
        await db.commit()
    await db.refresh(release)
    release_res = await db.execute(select(models.Release).where(
        models.Release.id == release.id).options(
        selectinload(models.Release.created_by),
        selectinload(models.Release.platform)
    ))
    return release_res.scalars().first()


async def commit_release(db: Session, release_id: int) -> (models.Release, str):
    async with db.begin():
        release_result = await db.execute(
            select(models.Release).where(
                models.Release.id == release_id).with_for_update()
        )
        release = release_result.scalars().first()
        if not release:
            raise DataNotFoundError(f'Release with ID {release_id} not found')
        builds_q = select(models.Build).where(
            models.Build.id.in_(release.build_ids))
        builds_result = await db.execute(builds_q)
        for build in builds_result.scalars().all():
            build.release = release
            db.add(build)
        release.status = ReleaseStatus.IN_PROGRESS
        db.add(release)
        await db.commit()
    try:
        await execute_release_plan(release_id, db)
    except (EmptyReleasePlan, MissingRepository, SignError) as e:
        message = f'Cannot commit release: {str(e)}'
        release.status = ReleaseStatus.FAILED
    else:
        message = 'Successfully committed release'
        release.status = ReleaseStatus.COMPLETED
    db.add(release)
    await db.commit()
    await db.refresh(release)
    release_res = await db.execute(select(models.Release).where(
        models.Release.id == release.id).options(
        selectinload(models.Release.created_by),
        selectinload(models.Release.platform)
    ))
    return release_res.scalars().first(), message
