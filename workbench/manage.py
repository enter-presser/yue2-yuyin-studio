import argparse
import getpass
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).parent/'vendor'))
from store import add_user
p=argparse.ArgumentParser()
p.add_argument('command',choices=['add-user'])
p.add_argument('username')
a=p.parse_args()
password=getpass.getpass('New password (12+ characters): ')
if len(password)<12: raise SystemExit('At least 12 characters required')
add_user(a.username,password)
print('User created. No public signup is enabled.')
