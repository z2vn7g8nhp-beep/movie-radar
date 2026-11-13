import ast
import copy
import re
from pathlib import Path
import unittest

# Load the actual Radar class without importing unrelated network dependencies.
module = ast.parse(Path('scraper/run.py').read_text())
cls = next(n for n in module.body if isinstance(n, ast.ClassDef) and n.name == 'Radar')
env = {'re':re, 'NOW_S':'2030-09-01T00:00:00+08:00', 'TODAY':'2030-09-01',
       'md':lambda value: value, 'buy_url':lambda *args:'https://m.maoyan.com/'}
exec(compile(ast.Module(body=[cls],type_ignores=[]), 'scraper/run.py', 'exec'), env)
Radar = env['Radar']

class SaleRoundTests(unittest.TestCase):
    def setUp(self):
        self.r = Radar.__new__(Radar)
        self.r.state = {'movies':{'1':{'rt':'2030-10-01'}}}
        self.r.events = []
        self.r.cfg = {'city':{'maoyan_ci':42}}
        self.r.ticket_mode = 'starred'
        self.starred = True
        self.r.watched = lambda *args: self.starred
        self.c = {'short':'测试影院'}
        self.reg = {'1':{'count':1,'shows':[]}}
        self.movie = {'nm':'测试电影','days':[{'date':'2030-09-30','shows':[
            {'seq':'s1','tm':'19:00','th':'IMAX','tp':'IMAX','available':True,'round':''}]}]}

    def scan(self):
        self.r.scan_movie(self.c,'25769',self.reg,False,[],'1',self.movie)

    def test_upgrade_baseline_and_stable_repeat_do_not_alert(self):
        self.scan();self.scan();self.assertEqual(self.r.events,[])

    def test_new_session_alerts_without_cancelling_star(self):
        self.scan()
        self.movie['days'][0]['shows'].append(dict(self.movie['days'][0]['shows'][0],seq='s2'))
        self.scan();self.scan()
        self.assertEqual(len(self.r.events),1)
        self.assertTrue(self.r.events[0]['_star'])
        self.assertTrue(self.starred)

    def test_sold_out_then_restock_alerts(self):
        self.scan();self.movie['days'][0]['shows'][0]['available']=False;self.scan()
        self.movie['days'][0]['shows'][0]['available']=True;self.scan()
        self.assertEqual(len(self.r.events),1)

    def test_new_round_alerts(self):
        self.scan();self.movie['days'][0]['shows'][0]['round']='new-round';self.scan()
        self.assertEqual(len(self.r.events),1)

    def test_missing_session_does_not_fake_restock(self):
        self.scan();old=copy.deepcopy(self.movie)
        self.movie['days'][0]['shows']=[];self.scan();self.movie=old;self.scan()
        self.assertEqual(self.r.events,[])

    def test_manual_unstar_stops_round_alerts(self):
        self.scan();self.starred=False
        self.movie['days'][0]['shows'][0]['round']='new-round';self.scan()
        self.assertEqual(self.r.events,[])

    def test_released_movie_never_alerts(self):
        self.scan();self.r.state['movies']['1']['rt']='2030-08-31'
        self.movie['days'][0]['shows'][0]['round']='new-round';self.scan()
        self.assertEqual(self.r.events,[])

if __name__ == '__main__':unittest.main()
