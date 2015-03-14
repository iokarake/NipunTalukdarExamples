import re
import pickle
import copy
from time import time
from thrift import Thrift
from thrift.transport import TTransport, TSocket
from thrift.protocol import TBinaryProtocol
from stormpy.storm import Nimbus, ttypes

clusterinfo = None
topology_found = True
descriptors = list()
topology = ''
topologies = []
serialfile_dir = '/tmp'
topology_summary_cols_map = {'status' : 'Status', 'num_workers' : 'Worker Count', \
        'num_executors' : 'Executor_count', 'uptime_secs': 'Uptime', 'num_tasks' : 'Task_count'}

spout_stats = {'Executors' : ['Count', '%u'], 'Tasks' : ['Count', '%u'],
                'Emitted' : ['Count', '%u'], 
                'Transferred' : ['Count', '%u'], 
                'CompleteLatency' : ['ms', '%f'],
                'Acked' : ['Count', '%u'],
                'Failed' : ['Count', '%f']}

bolt_stats = {'Executors' : ['Count', '%u'], 'Tasks' : ['Count', '%u'],
                'Emitted' : ['Count', '%u'], 
                'Executed' : ['Count', '%u'], 
                'Transferred' : ['Count', '%u'], 
                'ExecuteLatency' : ['ms', '%f'],
                'ProcessLatency' : ['ms', '%f'],
                'Acked' : ['Count', '%u'],
                'Failed' : ['Count', '%f']}

diff_cols = [ 'Acked', 'Failed', 'Executed', 'Transferred', 'Emitted' ]

overall = { 'Executor_count' : ['Count' , '%u'],
            'Worker_count' : ['Count', '%u'],
            'Task_count' : ['Count', '%u'],
            'Uptime_secs' : ['Count', '%u'] }

boltspoutstats = {}
overallstats = {}
component_task_count = {}
component_exec_count = {}
lastchecktime = 0
lastinfotime = 0
maxinterval = 6
bolt_array = []
spout_array = []

def get_avg(arr):
    if len(arr) < 1:
        return 0
    return sum(arr) / len(arr)

def normalize_stats(stats, duration):
    for k in stats:
        statsk = stats[k]
        if 'Emitted' in statsk and duration > 0:
            if statsk['Emitted'] > 0:
                statsk['Emitted'] = statsk['Emitted'] / duration
        if 'Acked' in statsk and duration > 0:
            if statsk['Acked'] > 0:
                statsk['Acked'] = statsk['Acked'] / duration
        if 'Executed' in statsk and duration > 0:
            print statsk['Executed']
            if statsk['Executed'] > 0:
                statsk['Executed'] = statsk['Executed'] / duration

    print stats
def freshen():
    global lastchecktime
    if time() > (lastchecktime + maxinterval):
        lastchecktime = time()
        boltspoutstats.clear()
        overallstats.clear()
        component_task_count.clear()
        component_exec_count.clear()
        get_topology_stats(topology)
        if not topology_found:
            return
        savedlastchecktime = 0
        tmpsavestats = None
        inf = None
        try:
            inf = open('/tmp/save_stats_for' + topology + '.pk', 'rb')
        except IOError as e:
            pass
        if inf is not None:
            try:
                tmpsavestats = pickle.load(inf)
                savedlastchecktime = pickle.load(inf)
            except EOFError as e:
                pass
            inf.close()
        of = open('/tmp/save_stats_for' + topology + '.pk', 'wb')
        if of is not None:
            pickle.dump(boltspoutstats, of)
            pickle.dump(lastchecktime, of)
            of.close()
        if overallstats['Uptime_secs'] > (lastchecktime - savedlastchecktime):
            if tmpsavestats is not None:
                for bolt in bolt_array:
                    if bolt in tmpsavestats and bolt in boltspoutstats:
                        stats_new = boltspoutstats[bolt]
                        stats_old = tmpsavestats[bolt]
                        for key in bolt_stats:
                            if key == 'ExecuteLatency' or key == 'ProcessLatency': continue
                            if key not in stats_new: continue
                            if key not in stats_old: continue
                            if key in diff_cols:
                                stats_new[key] -= stats_old[key]
                for spout in spout_array:
                    if spout in tmpsavestats and spout in boltspoutstats:
                        stats_new = boltspoutstats[spout]
                        stats_old = tmpsavestats[spout]
                        for key in spout_stats:
                            if key == 'CompleteLatency': continue
                            if key not in stats_new: continue
                            if key not in stats_old: continue
                            if key in diff_cols:
                                stats_new[key] -= stats_old[key]
                normalize_stats(boltspoutstats, lastchecktime - savedlastchecktime)
            else:
                normalize_stats(boltspoutstats, overallstats['Uptime_secs'])
        else:
            normalize_stats(boltspoutstats, overallstats['Uptime_secs'])
                

def callback_boltspout(name):
    freshen()
    if not topology_found:
        return -1
    bolt, statname = name.split('_')
    return boltspoutstats[bolt][statname]

def callback_overall(name):
    freshen()
    if not topology_found:
        return -1
    return overallstats[name]

def update_task_count(component_name, count):
    if component_name not in component_task_count:
        component_task_count[component_name] = 0
    component_task_count[component_name] += count
    
def update_exec_count(component_name, count):
    if component_name not in component_exec_count:
        component_exec_count[component_name] = 0
    component_exec_count[component_name] += count

def update_whole_num_stat_special(stats, store, boltname, statname):
    if  boltname not in store: 
        store[boltname] = {}
    if statname not in store[boltname]:
        store[boltname][statname] = 0
    for k in stats:
        if k == '__metrics' or k == '__ack_init' or k == '__ack_ack' or k == '__system': 
            continue
        store[boltname][statname] += stats[k]

def update_whole_num_stat(stats, store, boltname, statname):
    if  boltname not in store: 
        store[boltname] = {}
    if statname not in store[boltname]:
        store[boltname][statname] = 0
    for k in stats:
        store[boltname][statname] += stats[k]

def update_avg_stats(stats, store, boltname, statname):
    if  boltname not in store: 
        store[boltname] = {}
    if statname not in store[boltname]:
        store[boltname][statname] = []
    for k in stats:
        store[boltname][statname].append(stats[k])

def get_topology_stats_for(topologies):
    all_topology_stats.clear()
    for topology in topologies:
        all_topology_stats[topology] = get_topology_stats(topology)


def get_topology_stats(toplogyname):
    try:
        global topology_found
        global clusterinfo
        global lastinfotime
        topology_found = False
        transport = TSocket.TSocket('127.0.0.1' , 6627)
        transport.setTimeout(1000)
        framedtrasp = TTransport.TFramedTransport(transport)
        protocol = TBinaryProtocol.TBinaryProtocol(framedtrasp)
        client = Nimbus.Client(protocol)
        framedtrasp.open()
        if (lastinfotime + 4) < time():
            lastinfotime = time()
            clusterinfo = client.getClusterInfo()
        for tsummary in clusterinfo.topologies:
            if tsummary.name == toplogyname:
                topology_found = True
                overallstats['Executor_count'] = tsummary.num_executors
                overallstats['Task_count'] = tsummary.num_tasks
                overallstats['Worker_count'] = tsummary.num_workers
                overallstats['Uptime_secs'] = tsummary.uptime_secs
                tinfo = client.getTopologyInfo(tsummary.id)
                for exstat in tinfo.executors:
                    stats = exstat.stats
                    update_whole_num_stat_special(stats.emitted[":all-time"], boltspoutstats,
                            exstat.component_id, 'Emitted')
                    update_whole_num_stat_special(stats.transferred[":all-time"], boltspoutstats,
                            exstat.component_id, 'Transferred')

                    numtask = exstat.executor_info.task_end - exstat.executor_info.task_end + 1
                    update_task_count(exstat.component_id, numtask)
                    update_exec_count(exstat.component_id, 1)
                    if stats.specific.bolt is not None:
                        update_whole_num_stat(stats.specific.bolt.acked[":all-time"], boltspoutstats,
                                exstat.component_id, 'Acked')
                        update_whole_num_stat(stats.specific.bolt.failed[":all-time"], boltspoutstats,
                                exstat.component_id, 'Failed')
                        update_whole_num_stat(stats.specific.bolt.executed[":all-time"], boltspoutstats,
                                exstat.component_id, 'Executed')
                        update_avg_stats(stats.specific.bolt.process_ms_avg["600"], boltspoutstats,
                                exstat.component_id, 'process_ms_avg')
                        update_avg_stats(stats.specific.bolt.execute_ms_avg["600"], boltspoutstats,
                                exstat.component_id, 'execute_ms_avg')
                    if stats.specific.spout is not None:
                        update_whole_num_stat(stats.specific.spout.acked[":all-time"], boltspoutstats,
                                exstat.component_id, 'Acked')
                        update_whole_num_stat(stats.specific.spout.failed[":all-time"], boltspoutstats,
                                exstat.component_id, 'Failed')
                        update_avg_stats(stats.specific.spout.complete_ms_avg[":all-time"], boltspoutstats,
                                exstat.component_id, 'complete_ms_avg')
        
        if '__acker' in boltspoutstats:
            del boltspoutstats['__acker']
        for key in boltspoutstats:
            if 'complete_ms_avg' in boltspoutstats[key]:
                avg = get_avg(boltspoutstats[key]['complete_ms_avg'])
                boltspoutstats[key]['CompleteLatency'] = avg
                del boltspoutstats[key]['complete_ms_avg']
            if 'process_ms_avg' in boltspoutstats[key]:
                avg = get_avg(boltspoutstats[key]['process_ms_avg'])
                boltspoutstats[key]['ProcessLatency'] = avg
                del boltspoutstats[key]['process_ms_avg']
            if 'execute_ms_avg' in boltspoutstats[key]:
                avg = get_avg(boltspoutstats[key]['execute_ms_avg'])
                boltspoutstats[key]['ExecuteLatency'] = avg
                del boltspoutstats[key]['execute_ms_avg']

        for key in component_task_count:
            if key in boltspoutstats:
                boltspoutstats[key]['Tasks'] = component_task_count[key]
        for key in component_exec_count:
            if key in boltspoutstats:
                boltspoutstats[key]['Executors'] = component_exec_count[key]

        framedtrasp.close()

    except Exception as e:
        clusterinfo = None
        print e

def metric_init(params):
    global descriptors
    global topology
    groupname = 'Storm Topology'
    if 'topology' in params and len(params['topology']):
        groupname =  params['topology']
    else:
        return
    topology = groupname
    if 'spouts' in params:
        global spout_array
        spout_array = re.split('[,]+', params['spouts'])
        for spout in spout_array:
            for statname in spout_stats:
                d = {'name' : spout + '_' + statname, 'call_back' : callback_boltspout,
                    'time_max': 90,
                    'value_type': spout_stats[statname][0],
                    'units': 'Count',
                    'slope': 'both',
                    'format': spout_stats[statname][1],
                    'description': '',
                    'groups': groupname}
                descriptors.append(d)

    if 'bolts' in params:
        global bolt_array
        bolt_array = re.split('[,]+', params['bolts'])
        for bolt in bolt_array:
            for statname in bolt_stats:
                d = {'name' : bolt + '_' + statname, 'call_back' : callback_boltspout,
                    'time_max': 90,
                    'value_type': bolt_stats[statname][0],
                    'units': 'Count',
                    'slope': 'both',
                    'format': bolt_stats[statname][1],
                    'description': '',
                    'groups': groupname}
                descriptors.append(d)

    for key in overall:
        d = {'name' : key, 'call_back' : callback_overall,
            'time_max': 90,
            'value_type': overall[key][0],
            'units': 'Count',
            'slope': 'both',
            'format': overall[key][1],
            'description': '',
            'groups': groupname} 
        descriptors.append(d)

if __name__ == '__main__':
    params = {'spouts': 'SampleSpoutTwo', 'bolts' : 'boltc', 'topology' : 'SampleTopology'}
    metric_init(params)
    for d in descriptors:
        v = d['call_back'](d['name'])
        print 'OK', d['name'], v