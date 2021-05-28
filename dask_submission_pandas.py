import time
import os
import argparse
import traceback
import tqdm
import datetime
import coffea.processor as processor
from coffea.processor import dask_executor, run_uproot_job
from python.preprocessor import load_samples
from python.utils import mkdir

import dask
import dask.dataframe as dd
from dask.distributed import Client
dask.config.set({"temporary-directory": "/depot/cms/users/minxi/dask-temp/"})

parser = argparse.ArgumentParser()
# Slurm cluster IP to use. If not specified, will create a local cluster
parser.add_argument("-sl", "--slurm", dest="slurm_port",
                    default=None, action='store',
                    help='Slurm cluster port (if not specified, '
                    'will create a local cluster)')
parser.add_argument("-y", "--year", dest="year", default='2016',
                    action='store',
                    help='Year to process (2016, 2017 or 2018)')
parser.add_argument("-l", "--label", dest="label", default="test_march",
                    action='store',
                    help='Unique run label (to create output path)')
parser.add_argument("-ch", "--chunksize", dest="chunksize",
                    default=100000, action='store',
                    help='Approximate chunk size')
parser.add_argument("-mch", "--maxchunks", dest="maxchunks", default=-1,
                    action='store',
                    help='Max. number of chunks')
parser.add_argument("-cl", "--channel", dest="channel", default="mu",
                    action='store',
                    help='the flavor of the final state dilepton')


args = parser.parse_args()

node_ip = '128.211.149.135'
dash_local = f'{node_ip}:34875'


if args.slurm_port is None:
    local_cluster = True
    slurm_cluster_ip = ''
else:
    local_cluster = False
    slurm_cluster_ip = f'{node_ip}:{args.slurm_port}'

mch = None if int(args.maxchunks) < 0 else int(args.maxchunks)
dt=datetime.datetime.now()
local_time=str(dt.year)+'_'+str(dt.month)+'_'+str(dt.day)+'_'+str(dt.hour)+'_'+str(dt.minute)+'_'+str(dt.second)
parameters = {
    'year': args.year,
    'label': args.label,
    'global_out_path': '/depot/cms/users/minxi/NanoAOD_study/Zprime-mumu/output/',
    'out_path': f'{args.year}_{args.label}_{local_time}',
    #'server': 'root://xrootd.rcac.purdue.edu/',
    'server': 'root://cmsxrootd.fnal.gov//',
    'datasets_from': 'Zprime',
    'from_das': True,
    'chunksize': int(args.chunksize),
    'maxchunks': mch,
    'save_output': True,
    'local_cluster': local_cluster,
    'slurm_cluster_ip': slurm_cluster_ip,
    'client': None,
    'channel':args.channel,
}

parameters['out_dir'] = f"{parameters['global_out_path']}/"\
                        f"{parameters['out_path']}"


def saving_func(output, out_dir):
    from dask.distributed import get_worker
    name = None
    for key, task in get_worker().tasks.items():
        if task.state == "executing":
            name = key[-32:]
    if not name:
        return
    for ds in output.s.unique():
        df = output[output.s == ds]
        if df.shape[0] == 0:
            return
        mkdir(f'{out_dir}/{ds}')
        df.to_parquet(
            path=f'{out_dir}/{ds}/{name}.parquet',
        )    


def submit_job(arg_set, parameters):
    out_dir = f"{parameters['out_dir']}/"
    mkdir(out_dir)
    
    if parameters['channel'] == 'mu':
        from python.dimuon_processor import DimuonProcessor as event_processor
    elif parameters['channel'] == 'el':
        from python.dielectron_processor import DielectronProcessor as event_processor
    else:
        print('wrong channel input')    
    executor = dask_executor
    executor_args = {
        'client': parameters['client'],
        'schema': processor.NanoAODSchema,
        'use_dataframes': True,
        'retries': 0
    }
    processor_args = {
        'samp_info': parameters['samp_infos'],
        'do_timer': False,
    }
    try:
        output = run_uproot_job(parameters['samp_infos'].fileset, 'Events',
                                event_processor(**processor_args),
                                executor, executor_args=executor_args,
                                chunksize=parameters['chunksize'],
                                maxchunks=parameters['maxchunks'])

    except Exception as e:
        tb = traceback.format_exc()
        return 'Failed: '+str(e)+' '+tb

    return 'Success!'



if __name__ == "__main__":
    tick = time.time()
    smp = {
        # 'single_file': [
        #     'test_file',
        # ],
        'data': [
            'data_A',
            'data_B',
            'data_C',
            'data_D',
            #'data_E',
            #'data_F',
            #'data_G',
            #'data_H',
        ],
        'other_mc': [
            'WZ', 'WZ3LNu', 'WZ2L2Q', 
            'ZZ', 'ZZ2L2Nu', 'ZZ2L2Nu_ext', 'ZZ2L2Q', 'ZZ4L', 'ZZ4L_ext', 
            'WWinclusive', 'WW200to600', 'WW600to1200', 'WW1200to2500', 'WW2500',
            'dyInclusive50', 'Wjets', 
            'ttbar_lep', 'ttbar_lep_500to800_ext', 'ttbar_lep_500to800', 'ttbar_lep_800to1200', 'ttbar_lep_1200to1800', 'ttbar_lep_1800toInf', 
            'Wantitop', 'tW'
        ],
        'dy':[
            'dy120to200',
            'dy200to400','dy400to800',
            'dy800to1400','dy1400to2300','dy2300to3500',
            'dy3500to4500','dy4500to6000','dy6000toInf',

        ],
    }

    if parameters['local_cluster']:
        parameters['client'] = dask.distributed.Client(
                                   processes=True,
                                    n_workers=48,
                                    #dashboard_address=dash_local,
                                    threads_per_worker=1,
                                    memory_limit='2.9GB',
                                )
    else:
        parameters['client'] = Client(
            parameters['slurm_cluster_ip'],
        )
    print('Client created')

    datasets_mc = []
    datasets_data = []
    for group, samples in smp.items():
        for sample in samples:
            if sample in ['WZ', 'tW', 'WZ2L2Q', 'Wantitop']:
                continue
            if group != 'dy':
                continue
            #if sample in ['dy200to400','dy800to1400']:
            #    continue
            if group == 'data':
                datasets_data.append(sample)
            else:
                datasets_mc.append(sample)

    timings = {}

    to_process = {
        'MC': datasets_mc,
        'DATA': datasets_data
    }
    for lbl, datasets in to_process.items():
        if len(datasets) == 0:
            continue
        print(f'Processing {lbl}')
        arg_sets = []
        for d in datasets:
            arg_sets.append({'dataset': d})

        tick1 = time.time()
        parameters['samp_infos'] = load_samples(datasets, parameters)
        timings[f'load {lbl}'] = time.time() - tick1

        tick2 = time.time()
        out = submit_job({}, parameters)
        timings[f'process {lbl}'] = time.time() - tick2

        print(out)

    elapsed = round(time.time() - tick, 3)
    print(f'Finished everything in {elapsed} s.')
    print('Timing breakdown:')
    print(timings)
