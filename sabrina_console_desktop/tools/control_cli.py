"""Submit and inspect shared Sabrina objectives from any client workspace."""
import argparse
import json
from pathlib import Path
from tools.control_client import configured


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['submit','status','cancel','request','cancel-request','reconcile'])
    parser.add_argument('--id')
    parser.add_argument('--input',help='JSON objective brief')
    parser.add_argument('--client',default='operator')
    parser.add_argument('--project',default='research')
    parser.add_argument('--resource')
    parser.add_argument('--confirmed-stopped',action='store_true')
    args=parser.parse_args()
    client=configured()
    if client is None:parser.error('Shared coordinator is not configured')
    client.client,client.project=args.client,args.project
    if args.command=='submit':
        if not args.input:parser.error('--input is required')
        brief=json.loads(Path(args.input).read_text(encoding='utf-8-sig'))
        result=client.submit(**brief)
    elif args.command=='status':
        result=client.status(args.id) if args.id else client.request('/v1/status')
    elif args.command=='cancel':
        result=client.cancel(args.id)
    elif args.command=='request':
        result=client.request('/v1/inference/'+args.id)
    elif args.command=='cancel-request':
        result=client.request('/v1/inference/'+args.id+'/cancel',{})
    else:
        result=client.request('/v1/leases/reconcile',{'resource':args.resource,'confirmed_stopped':args.confirmed_stopped})
    print(json.dumps(result,indent=2))


if __name__=='__main__':main()
