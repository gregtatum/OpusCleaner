#!/usr/bin/env python3
import asyncio
import gzip
import hashlib
import json
import os
import re
import subprocess
import sys
import traceback
from contextlib import ExitStack
from enum import Enum
from glob import glob
from itertools import chain, zip_longest
from pprint import pprint
from shutil import copyfileobj
from tempfile import TemporaryFile
from typing import NamedTuple, Optional, Iterable, TypeVar, Union, Literal, Any, AsyncIterator, Awaitable, cast, IO, List, Dict, Tuple, AsyncIterator
from warnings import warn

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, parse_obj_as, validator, ValidationError
from starlette.datastructures import URL
from starlette.responses import FileResponse, RedirectResponse, Response
from starlette.types import Scope

from opuscleaner._util import none_throws
from opuscleaner.categories import app as categories_app
from opuscleaner.config import DATA_PATH, FILTER_PATH, COL_PY, SAMPLE_PY, SAMPLE_SIZE
from opuscleaner.datasets import list_datasets, Path
from opuscleaner.download import app as download_app
from opuscleaner.filters import filter_format_command, get_global_filter, get_global_filters, set_global_filters, list_filters, FilterType, FilterStep, FilterPipeline
from opuscleaner.sample import sample


import mimetypes
mimetypes.add_type('application/javascript', '.js')


FRONTEND_PATH = next(iter(path
    for path in [
        os.path.join(os.path.dirname(__file__), 'frontend'),
        os.path.join(os.path.dirname(__file__), '../frontend/dist'),
    ]
    if os.path.exists(path)
))


class File(BaseModel):
    path: str
    size: int


class Dataset(BaseModel):
    name: str
    columns: Dict[str,File]


class FilterPipelinePatch(BaseModel):
    """A list of changes to a filter pipeline (used when updating filters)"""
    filters: List[FilterStep]


def dataset_path(name:str, template:str) -> str:
    # TODO: fix this hack to get the file path from the name this is silly we
    # should just use get_dataset(name).path or something
    root = DATA_PATH.split('*')[0]

    # If the dataset name is a subdirectory, do some hacky shit to get to a
    # .sample.gz file in said subdirectory.
    parts = name.rsplit('/', maxsplit=2)
    if len(parts) == 2:
        root = os.path.join(root, parts[0])
        filename = parts[1]
    else:
        filename = parts[0]

    return os.path.join(root, template.format(filename))


def sample_path(name:str, langs:Iterable[str]) -> str:
    languages = '.'.join(sorted(langs))
    return dataset_path(name, f'.sample.{{}}.{languages}')


def filter_configuration_path(name:str) -> str:
    return dataset_path(name, '{}.filters.json')


async def compute_sample(name:str, columns:List[Tuple[str,Path]]) -> None:
    langs = [lang for lang, _ in columns]
    with TemporaryFile() as tempfile:
        proc = await asyncio.subprocess.create_subprocess_exec(
            *SAMPLE_PY,
            '-n', str(SAMPLE_SIZE),
            *[str(file.resolve()) for _, file in columns],
            stdout=tempfile,
            stderr=asyncio.subprocess.PIPE)

        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            raise Exception(f'sample.py returned {proc.returncode}: {stderr.decode()}')

        tempfile.seek(0)

        with open(sample_path(name, langs), 'wb') as fdest:
            copyfileobj(tempfile, fdest)


class FilterOutput(NamedTuple):
    langs: List[str] # order of columns
    returncode: int
    stdout: bytes
    stderr: bytes


class ParsedFilterOutput(BaseModel):
    """JSON serializable version of FilterOutput that has stdout parsed into
       an array of dicts, with a field per language.
    """
    returncode: int
    stdout: List[Dict[str,str]]
    stderr: str
    
    def __init__(self, output:FilterOutput):
        lines = []

        for lineno, line in enumerate(output.stdout.rstrip(b'\n').split(b'\n'), start=1):
            values = []
            for colno, field in enumerate(line.split(b'\t'), start=1):
                try:
                    values.append(field.decode())
                except UnicodeDecodeError as e:
                    values.append(f'[Error: Cannot decode line {lineno} column {colno}: {e!s}]')
            lines.append(dict(zip_longest(output.langs, values, fillvalue='')))

        super().__init__(
            returncode=output.returncode,
            stdout=lines,
            stderr=output.stderr.decode())


class SampleCacheEntry(NamedTuple):
    checksum: bytes
    future: asyncio.Task[FilterOutput]


sample_cache: Dict[str,List[SampleCacheEntry]] = {}


def cache_hash(obj: Any, seed: bytes = bytes()) -> bytes:
    impl = hashlib.sha256(seed)
    impl.update(json.dumps(obj, sort_keys=True).encode())
    return impl.digest()


async def get_dataset_sample(name:str, columns:List[Tuple[str,Path]]) -> FilterOutput:
    langs = [lang for lang, _ in columns]

    if not os.path.exists(sample_path(name, langs)):
        await compute_sample(name, columns)

    with open(sample_path(name, langs), 'rb') as fh:
        stdout = fh.read()

    return FilterOutput([lang for lang, _ in columns], 0, stdout, bytes())


def format_shell(val: Any) -> str:
    if isinstance(val, bool):
        return '1' if val else ''
    elif isinstance(val, tuple):
        raise NotImplementedError()
    elif isinstance(val, list):
        raise NotImplementedError()
    else:
        return str(val)


async def exec_filter_step(filter_step: FilterStep, langs: List[str], input: bytes) -> Tuple[bytes,bytes]:
    filter_definition = get_global_filter(filter_step.filter)

    command = filter_format_command(filter_definition, filter_step, langs)

    # Make sure the path to the python binary (and the installed utils)
    # is in the PATH variable. If you load a virtualenv this happens by
    # default, but if you call it with the virtualenv's python binary 
    # directly it wont.
    pyenv_bin_path = os.path.dirname(sys.executable)
    os_env_bin_paths = os.environ.get('PATH', '').split(os.pathsep)
    filter_env = {
        **os.environ,
        'PATH': os.pathsep.join([pyenv_bin_path] + os_env_bin_paths)
    } if pyenv_bin_path not in os_env_bin_paths else None
    
    p_filter = await asyncio.create_subprocess_shell(command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=filter_definition.basedir,
        env=filter_env)

    # Check exit codes, testing most obvious problems first.
    stdout, stderr = await p_filter.communicate(input=input)

    return FilterOutput(langs, p_filter.returncode, stdout, stderr)


def cancel_cached_tasks(name:str, offset:int) -> None:
    for entry in sample_cache[name][offset:]:
        entry.future.cancel()
    del sample_cache[name][offset:]


async def get_sample(name:str, filters:List[FilterStep]) -> AsyncIterator[FilterOutput]:
    columns: List[Tuple[str,Path]] = sorted(list_datasets(DATA_PATH)[name].items(), key=lambda pair: pair[0])
    langs = [lang for lang, _ in columns]

    checksum = cache_hash([
        (name, str(path), path.stat().st_mtime)
        for name, path in columns
    ])

    # If we don't have a sample stored, generate one. Doing it in bytes because
    # it might save us parsing utf-8 (also assumptions! It it utf-8?)
    if not name in sample_cache or sample_cache[name][0].checksum != checksum:
        sample_cache[name] = [
            SampleCacheEntry(
                checksum=checksum,
                future=asyncio.create_task(get_dataset_sample(name, columns))
            )
        ]

    sample = await sample_cache[name][0].future

    # Return a clean unfiltered sample first
    yield sample

    for i, filter_step in enumerate(filters, start=1):
        filter_definition = get_global_filter(filter_step.filter)

        checksum = cache_hash(jsonable_encoder(filter_step),
            cache_hash(jsonable_encoder(filter_definition),
                sample_cache[name][i-1].checksum))

        # If we do not have a cache entry for this point
        if len(sample_cache[name]) <= i or sample_cache[name][i].checksum != checksum:
            # Invalidate all the cache after this step
            cancel_cached_tasks(name, i)

            sample_cache[name].append(SampleCacheEntry(
                checksum=checksum,
                future=asyncio.create_task(exec_filter_step(filter_step, langs, sample.stdout))
            ))

            assert len(sample_cache[name]) == i + 1
        
        sample = await sample_cache[name][i].future    
        
        # Return the (partially) filtered sample
        yield sample


    # if there are additional steps left in the cache, remove them
    if len(sample_cache[name]) > len(filters) + 1:
        cancel_cached_tasks(name, len(filters) + 1)


def stream_jsonl(iterable:AsyncIterator[Any]) -> StreamingResponse:
    return StreamingResponse(
        (
            json.dumps(jsonable_encoder(line), separators=(',', ':')).encode() + b"\n"
            async for line in iterable
        ),
        media_type='application/json')


app = FastAPI()

@app.get('/api/datasets/')
def api_list_datasets() -> List[Dataset]:
    return [
        Dataset(name=name, columns={
            lang: File(path=file.name, size=file.stat().st_size)
            for lang, file in columns.items()
        })
        for name, columns in list_datasets(DATA_PATH).items()
    ]


@app.get('/api/datasets/{name:path}/')
def api_get_dataset(name:str) -> Dataset:
    columns = list_datasets(DATA_PATH).get(name)

    if not columns:
        raise HTTPException(status_code=404, detail='Dataset not found')

    return Dataset(name=name, columns={
        lang: File(path=file.name, size=file.stat().st_size)
        for lang, file in columns.items()
    })


@app.get('/api/datasets/{name:path}/sample')
async def api_get_sample(name:str) -> Response:
    return stream_jsonl(ParsedFilterOutput(output) async for output in get_sample(name, []))


@app.post('/api/datasets/{name:path}/sample')
async def api_get_filtered_sample(name:str, filters:List[FilterStep]) -> Response:
    return stream_jsonl(ParsedFilterOutput(output) async for output in get_sample(name, filters))


def make_pipeline(name:str, filters:List[FilterStep] = []) -> FilterPipeline:
    columns = list_datasets(DATA_PATH)[name]
    return FilterPipeline(
        version=1,
        files=[file.name
            for _, file in
            sorted(columns.items(), key=lambda pair: pair[0])
        ],
        filters=filters
    )


@app.get('/api/datasets/{name:path}/configuration.json')
def api_get_dataset_filters(name:str) -> FilterPipeline:

    if not os.path.exists(filter_configuration_path(name)):
        return make_pipeline(name)

    with open(filter_configuration_path(name), 'r') as fh:
        data = json.load(fh)
        try:
            return parse_obj_as(FilterPipeline, data)
        except ValidationError:
            try:
                # Backwards compatibility
                return make_pipeline(name, parse_obj_as(List[FilterStep], data))
            except ValidationError:
                # Last resort case
                return make_pipeline(name)



@app.patch('/api/datasets/{name:path}/configuration.json')
def api_update_dataset_filters(name:str, patch:FilterPipelinePatch):
    pipeline = make_pipeline(name, patch.filters)
    with open(filter_configuration_path(name), 'w') as fh:
        return json.dump(pipeline.dict(), fh, indent=2)


@app.get('/api/datasets/{name:path}/configuration-for-opusfilter.yaml')
def api_get_dataset_filters_as_openfilter(name:str) -> Response:
    if not os.path.exists(filter_configuration_path(name)):
        raise HTTPException(status_code=404, detail='Dataset not found')

    with open(filter_configuration_path(name), 'r') as fh:
        data = json.load(fh)

    pipeline = parse_obj_as(FilterPipeline, data)

    opusfilter_config: Dict[str,Any] = {
        'steps': []
    }

    input_files = pipeline.files
    
    preprocess_steps = []

    filter_steps: List[Dict[str,Any]] = []

    for step in pipeline.filters:
        if (match := re.search(r'\bopusfilter\.preprocessors\.(\w+)\b', get_global_filter(step.filter).command)):
            preprocess_steps.append({
                str(match.group(1)): step.parameters
            })
        elif (match := re.search(r'\bopusfilter\.filters\.(\w+)\b', get_global_filter(step.filter).command)):
            filter_steps.append({
                str(match.group(1)): step.parameters
            })
        elif get_global_filter(step.filter).type == FilterType.BILINGUAL:
            filter_steps.append({
                'OpusCleanerFilter': {
                    'filter': step.filter,
                    'parameters': step.parameters
                },
                'module': 'opuscleaner.opusfilter_compat'
            })
        elif get_global_filter(step.filter).type == FilterType.MONOLINGUAL:
            filter_steps.append({
                'OpusCleanerFilter': {
                    'filter': step.filter,
                    'parameters': step.parameters
                },
                'module': 'opuscleaner.opusfilter_compat'
            })
        else:
            raise ValueError(f'Cannot convert "{step.filter}" to opusfilter configuration')

    if preprocess_steps:
        output_files = [
            os.path.join(os.path.dirname(file), 'preprocessed.' + os.path.basename(file))
            for file in pipeline.files
        ]

        opusfilter_config['steps'].append({
            'type': 'preprocess',
            'parameters': {
                'inputs': input_files,
                'outputs': output_files,
                'preprocessors': preprocess_steps
            }
        })

        input_files = output_files

    if filter_steps:
        output_files = [
            os.path.join(os.path.dirname(file), 'filtered.' + os.path.basename(file))
            for file in pipeline.files
        ]

        opusfilter_config['steps'].append({
            'type': 'filter',
            'parameters': {
                'inputs': input_files,
                'outputs': output_files,
                'filters': filter_steps
            }
        })

        input_files = output_files

    return Response(yaml.safe_dump(opusfilter_config, sort_keys=False), media_type='application/yaml')


@app.get('/api/filters/')
def api_get_filters():
    set_global_filters(list_filters(FILTER_PATH))
    return get_global_filters()


@app.get('/')
def redirect_to_interface():
    return RedirectResponse('/frontend/index.html')


app.mount('/frontend/', StaticFiles(directory=FRONTEND_PATH, html=True), name='static')

app.mount('/api/download/', download_app)

app.mount('/api/categories/', categories_app)

def main_serve(args):
    import uvicorn
    uvicorn.run(f'opuscleaner.server:app', port=args.port, reload=args.reload, log_level='info')


async def sample_all_datasets(args):
    tasks = []

    for name, columns in list_datasets(DATA_PATH).items():
        sorted_cols = sorted(columns.items(), key=lambda pair: pair[0])
        langs = [lang for lang, _ in sorted_cols]
        if not os.path.exists(sample_path(name, langs)):
            print(f"Sampling {name}...", file=sys.stderr)
            tasks.append([name, sorted_cols])

    for task, result in zip(tasks, await asyncio.gather(*[compute_sample(*task) for task in tasks], return_exceptions=True)):
        if isinstance(result, Exception):
            print(f"Could not compute sample for {task[0]}: {result!s}", file=sys.stderr)


def main_sample(args):
    asyncio.run(sample_all_datasets(args))


def main_list_commands(args):
    print("Error: No command specified.\n\n"
          "Available commands:\n"
          "  serve      run webserver\n"
          "  sample     sample all datasets\n"
          "", file=sys.stderr)
    sys.exit(1)


def main(argv=sys.argv):
    import argparse

    parser = argparse.ArgumentParser(description='Fill up those seats on your empty train.')
    parser.set_defaults(func=main_list_commands)
    subparsers = parser.add_subparsers()

    parser_serve = subparsers.add_parser('serve')
    parser_serve.add_argument('-p', '--port', type=int, default=8000, help='Bind socket to this port. (default: 8000)')
    parser_serve.add_argument('--reload', action='store_true', help='Enable auto-reload.')
    parser_serve.set_defaults(func=main_serve)

    parser_sample = subparsers.add_parser('sample')
    parser_sample.set_defaults(func=main_sample)

    args = parser.parse_args()

    set_global_filters(list_filters(FILTER_PATH))
    args.func(args)

if __name__ == '__main__':
    main()
