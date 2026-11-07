#!/usr/bin/env python3
"""Install project-local Qwen worker skills without replacing existing files."""
import argparse
import os
from pathlib import Path
import shutil

UNITY_SKILLS = ('unity-cli', 'unity-pipeline', 'sprite-editor', 'create-2d-physics',
                'ui-ugui', 'optimize-text-mesh-pro', 'audio-setup-mixers',
                'optimize-audio', 'manage-sprite-atlas', 'urp-postprocessing',
                'unity-package-management')


def install(project, home):
    if not project.is_dir():
        raise ValueError(f'Project directory does not exist: {project}')
    sources = [('swim-lane-worker', Path(__file__).parent / 'skills/swim-lane-worker')]
    if (project / 'ProjectSettings/ProjectVersion.txt').is_file():
        sources += [(name, home / '.agents/skills' / name) for name in UNITY_SKILLS
                    if (home / '.agents/skills' / name / 'SKILL.md').is_file()]
    missing = [str(source) for _, source in sources if not (source / 'SKILL.md').is_file()]
    if missing:
        raise ValueError('Missing skill sources (nothing installed): ' + ', '.join(missing))
    target = project / '.qwen/skills'
    for name, source in sources:
        dest = target / name
        if os.path.lexists(dest):
            if not (dest / 'SKILL.md').is_file():
                raise ValueError(f'Existing skill is invalid; left untouched: {dest}')
    qwen = project / 'QWEN.md'
    if qwen.is_symlink() or (qwen.exists() and not qwen.is_file()):
        raise ValueError('QWEN.md must be a regular file; left untouched')
    target.mkdir(parents=True, exist_ok=True)
    for name, source in sources:
        dest = target / name
        if os.path.lexists(dest):
            print(f'Kept existing: {dest.relative_to(project)}')
        else:
            shutil.copytree(source, dest)
            print(f'Installed: {dest.relative_to(project)}')
    planner_source = Path(__file__).parent / 'skills/swim-lane-planner'
    for relative in ('skills/donut-planner', '.claude/skills/donut-planner', '.cursor/skills/donut-planner'):
        dest = project / relative
        if not os.path.lexists(dest):
            shutil.copytree(planner_source, dest)
            print(f'Installed: {relative}')
    for lane in ('to_do', 'in_progress', 'done'):
        folder = project / 'swim_lanes' / lane
        folder.mkdir(parents=True, exist_ok=True)
        (folder / '.gitkeep').touch(exist_ok=True)
    text = qwen.read_text(encoding='utf-8') if qwen.exists() else '# Qwen project instructions\n'
    imports = ['@./.qwen/skills/swim-lane-worker/SKILL.md']
    rules = project / 'skills/project-rules/SKILL.md'
    if rules.is_file():
        imports.append('@./skills/project-rules/SKILL.md')
    additions = [line for line in imports if line[2:] not in text]
    if additions:
        qwen.write_text(text.rstrip() + '\n\n## Swim-lane worker\n\n' + '\n\n'.join(additions) + '\n')
    print(f'Ready: {project}\nStart a fresh Qwen session to load the installed skills.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['add-skills'])
    parser.add_argument('-p', '--project', default='.')
    args = parser.parse_args()
    try:
        install(Path(args.project).expanduser().resolve(), Path.home())
    except (ValueError, OSError) as error:
        parser.exit(1, f'{error}\n')


if __name__ == '__main__':
    main()
