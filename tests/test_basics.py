"""Unit and route smoke tests. Run with `python -m unittest discover -s tests -v`."""
import json
import os
import re
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ['SESSION_SECRET']='test-only-key-at-least-thirty-two-characters'
os.environ['ADMIN_PASSWORD']='test-only-admin-password-not-public'
os.environ['PRINTHARBOR_IMAGE_NETWORK']='0'
_temp=tempfile.TemporaryDirectory(prefix='printharbor-test-')
os.environ['DATA_DIR']=_temp.name

from app import images, mailer, printing, store, supplies, tailnet

class CoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        store.initialize()

    def test_commands(self):
        self.assertEqual(mailer.parse_command('PRINT PIN:abcDEF12 DUPLEX COPIES=3','abcdef12'),
                         {'sides':'two-sided-long-edge','copies':3})
        self.assertEqual(mailer.parse_command('PRINT PIN:abcDEF12 SHORT EDGE','abcdef12')['sides'],
                         'two-sided-short-edge')
        self.assertIsNone(mailer.parse_command('PRINT DUPLEX','abcdef12'))
        self.assertIsNone(mailer.parse_command('PRINT PIN:abcDEF12 COPIES=99','abcdef12'))
        self.assertEqual(mailer.parse_command('PRINT DUPLEX COPIES=2',''),
                         {'sides':'two-sided-long-edge','copies':2})
        self.assertIsNone(mailer.parse_command('COPIES=99',''))

    def test_sender_and_pin(self):
        msg=EmailMessage()
        msg['From']='Trusted <trusted@example.com>'
        msg['To']='print@example.com'
        msg['Subject']='PRINT PIN:abcdef12'
        cfg={'allowed_senders':'trusted@example.com','mail_recipient':'print@example.com','email_pin':'abcdef12','default_sides':'one-sided'}
        self.assertTrue(mailer.allowed_message(msg,cfg)[0])
        msg.replace_header('From','attacker@example.com')
        self.assertFalse(mailer.allowed_message(msg,cfg)[0])

    def test_pdf_preparation_and_limit(self):
        from reportlab.pdfgen import canvas
        import io
        buf=io.BytesIO()
        cv=canvas.Canvas(buf); cv.drawString(70,700,'hello');cv.showPage();cv.save()
        path,pages=printing.prepare_bytes('hello.pdf',buf.getvalue(),1)
        self.assertEqual(pages,1)
        self.assertTrue(Path(path).exists())
        Path(path).unlink()
        with self.assertRaises(printing.PrintError):
            printing.prepare_bytes('hello.pdf',buf.getvalue(),0)

    def test_email_to_print_pipeline_mock_cups(self):
        from reportlab.pdfgen import canvas
        from types import SimpleNamespace
        import io
        with store.connection() as db:
            db.execute("INSERT OR IGNORE INTO printers(queue,label,host,protocol) VALUES('Dads_Brother','Dad','192.168.50.40','ipp')")
        pdf=io.BytesIO()
        doc=canvas.Canvas(pdf); doc.drawString(50,700,'Email print'); doc.showPage();doc.save()
        msg=EmailMessage()
        msg['From']='trusted@example.com'; msg['To']='print@example.com'
        msg['Subject']='PRINT PIN:abcdef12 DUPLEX COPIES=2'
        msg.set_content('Here is the file')
        msg.add_attachment(pdf.getvalue(),maintype='application',subtype='pdf',filename='attachment.pdf')
        cfg={'allowed_senders':'trusted@example.com','mail_recipient':'print@example.com',
             'email_pin':'abcdef12','default_sides':'one-sided','max_files':'5',
             'default_queue':'Dads_Brother'}
        with patch.object(printing,'printer_reachable',return_value=True), patch.object(printing,'cmd',return_value=SimpleNamespace(returncode=0,stdout='request id is Dads_Brother-34 (1 file(s))',stderr='')) as fake:
            out=mailer.handle_message(msg,'test-mail-uid-1',cfg)
            self.assertEqual(out['jobs'],1)
            self.assertIn('sides=two-sided-long-edge',fake.call_args.args)
            self.assertIn('2',fake.call_args.args)
            out2=mailer.handle_message(msg,'test-mail-uid-1',cfg)
            self.assertEqual(out2['jobs'],1)
            self.assertEqual(fake.call_count,1)  # duplicate never re-submitted

    def test_each_attachment_is_its_own_duplex_job(self):
        from reportlab.pdfgen import canvas
        from pypdf import PdfReader
        import io
        with store.connection() as db:
            db.execute("INSERT OR IGNORE INTO printers(queue,label,host,protocol) VALUES('Office','Office','192.168.1.20','ipp')")

        def one_page(text):
            buf=io.BytesIO()
            doc=canvas.Canvas(buf)
            doc.drawString(50,700,text)
            doc.showPage()
            doc.save()
            return buf.getvalue()

        two=io.BytesIO()
        even=canvas.Canvas(two)
        even.drawString(50,700,'page one')
        even.showPage()
        even.drawString(50,700,'page two')
        even.showPage()
        even.save()
        even_path, even_pages=printing.prepare_bytes('even.pdf', two.getvalue(), 5)
        self.assertEqual(even_pages, 2)
        self.assertEqual(printing.sheet_for_duplex(even_path, 'two-sided-long-edge'), even_path)
        odd_path, odd_pages=printing.prepare_bytes('odd.pdf', one_page('only'), 5)
        self.assertEqual(odd_pages, 1)
        padded=printing.sheet_for_duplex(odd_path, 'two-sided-short-edge')
        self.assertNotEqual(padded, odd_path)
        self.assertEqual(len(PdfReader(padded).pages), 2)
        self.assertEqual(printing.sheet_for_duplex(odd_path, 'one-sided'), odd_path)
        Path(padded).unlink(missing_ok=True)
        Path(even_path).unlink(missing_ok=True)
        Path(odd_path).unlink(missing_ok=True)

        msg=EmailMessage()
        msg['From']='trusted@example.com'
        msg['To']='print@example.com'
        msg['Subject']='PRINT DUPLEX'
        msg.set_content('two files')
        msg.add_attachment(one_page('first'), maintype='application', subtype='pdf', filename='first.pdf')
        msg.add_attachment(one_page('second'), maintype='application', subtype='pdf', filename='second.pdf')
        cfg={'allowed_senders':'','mail_recipient':'','email_pin':'','default_sides':'one-sided','max_files':'5','default_queue':'Office'}
        seen=[]

        def fake_cmd(*args, **kwargs):
            path=args[-1]
            seen.append((Path(path).name, len(PdfReader(path).pages)))
            return SimpleNamespace(returncode=0, stdout='request id is Office-9 (1 file(s))', stderr='')

        with patch.object(printing, 'printer_reachable', return_value=True), patch.object(printing, 'cmd', side_effect=fake_cmd):
            out=mailer.handle_message(msg, 'two-files-1', cfg)
        self.assertEqual(out['jobs'], 2)
        self.assertEqual(len(seen), 2)
        self.assertTrue(all(name.endswith('.sheet.pdf') for name, _pages in seen))
        self.assertEqual([pages for _name, pages in seen], [2, 2])
        self.assertNotEqual(seen[0][0], seen[1][0])

    def test_rejected_mail_is_listed_with_the_reason(self):
        msg=EmailMessage()
        msg['From']='Stranger <stranger@example.com>'
        msg['To']='print@example.com'
        msg['Subject']='Please print this'
        cfg={'allowed_senders':'trusted@example.com','mail_recipient':'','email_pin':'','default_sides':'one-sided','default_queue':'Office','max_files':'5','print_email_body':'0'}
        first=mailer.handle_message(msg,'reject-uid-1',cfg)
        self.assertFalse(first['accepted'])
        mailer.handle_message(msg,'reject-uid-1',cfg)
        empty=EmailMessage()
        empty['From']='person@example.com'
        empty['Subject']='PRINT DUPLEX'
        empty.set_content('body only')
        open_cfg=dict(cfg, allowed_senders='', email_pin='')
        second=mailer.handle_message(empty,'reject-uid-2',open_cfg)
        self.assertEqual(second['reason'],'Email contained no supported attachments')
        with store.connection() as db:
            rows=db.execute("SELECT dedupe,status,error,filename,sender FROM jobs WHERE dedupe IN ('reject-uid-1','reject-uid-2') ORDER BY dedupe").fetchall()
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0]['status'],'rejected')
        self.assertEqual(rows[0]['filename'],'Please print this')
        self.assertEqual(rows[0]['sender'],'stranger@example.com')
        self.assertIn('allowlisted', rows[0]['error'])
        self.assertEqual(rows[1]['status'],'rejected')
        self.assertIn('no supported attachments', rows[1]['error'])
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as client:
            client.post('/login',data={'username':'admin','password':os.environ['ADMIN_PASSWORD']},follow_redirects=False)
            page=client.get('/?page=jobs')
            self.assertIn('Please print this', page.text)
            self.assertIn('Sender is not allowlisted', page.text)
            self.assertIn('Email contained no supported attachments', page.text)
            self.assertIn('>Rejected<', page.text)
            self.assertIn('Rejected mail stays in this log', page.text)

    def test_queue_input_validation(self):
        with self.assertRaises(printing.PrintError):
            printing.add_printer('Bad','../evil','127.0.0.1','ipp')

    def test_driver_match_uses_identity_and_falls_back(self):
        drivers=printing.parse_lpinfo('\n'.join([
            '{',
            'name = everywhere',
            'make-and-model = IPP Everywhere',
            'device-id =',
            '}',
            '{',
            'name = drv:///sample.drv/generic.ppd',
            'make-and-model = Generic PostScript',
            'device-id =',
            '}',
            '{',
            'name = drv:///brlaser.drv/brl2380d.ppd',
            'make-and-model = Brother HL-L2380DW series, using brlaser v6.2.8',
            'device-id = MFG:Brother;MDL:HL-L2380DW series;',
            '}',
            '{',
            'name = gutenprint.5.3://epson/epson-et-2760',
            'make-and-model = Epson ET-2760',
            'device-id = MFG:Epson;MDL:ET-2760;',
            '}',
        ]))
        brother=printing.choose_driver({
            'reachable':True,'ipp':True,'socket':True,'make':'Brother',
            'model':'HL-L2380DW series','identity':'Brother HL-L2380DW series',
        }, drivers)
        self.assertEqual(brother['model'],'drv:///brlaser.drv/brl2380d.ppd')
        self.assertEqual(brother['protocol'],'socket')
        self.assertEqual(brother['driver'],'Brother HL-L2380DW series, using brlaser v6.2.8')
        epson=printing.choose_driver({
            'reachable':True,'ipp':False,'socket':True,'make':'Epson','model':'ET-2760','identity':'Epson ET-2760',
        }, drivers)
        self.assertEqual(epson['model'],'gutenprint.5.3://epson/epson-et-2760')
        self.assertEqual(epson['protocol'],'socket')
        self.assertEqual(epson['driver'],'Epson ET-2760')
        described=printing.choose_driver({
            'reachable':True,'ipp':True,'socket':False,'make':'HP','model':'OfficeJet Pro 9020','identity':'HP OfficeJet Pro 9020',
        }, drivers)
        self.assertEqual(described['model'],'everywhere')
        self.assertEqual(described['protocol'],'ipp')
        self.assertNotIn('generic.ppd', described['model'])
        fields=printing.device_fields('MFG:Brother;CMD:PJL;MDL:HL-L2380DW series;')
        self.assertEqual(fields['MFG'],'Brother')
        self.assertEqual(fields['MDL'],'HL-L2380DW series')
        payload=b'\x02\x00\x00\x00\x00\x00\x00\x01\x04'
        payload+=bytes([0x41])+len('printer-make-and-model').to_bytes(2,'big')+b'printer-make-and-model'
        payload+=len('Brother HL-L2380DW').to_bytes(2,'big')+b'Brother HL-L2380DW'+b'\x03'
        parsed=printing.parse_ipp(b'HTTP/1.1 200 OK\r\n\r\n'+payload)
        self.assertEqual(parsed['printer-make-and-model'],['Brother HL-L2380DW'])
        numbers=b'\x02\x00\x00\x00\x00\x00\x00\x01\x04'
        numbers+=bytes([0x23])+len('printer-state').to_bytes(2,'big')+b'printer-state'+(4).to_bytes(2,'big')+(4).to_bytes(4,'big')
        numbers+=bytes([0x21])+len('marker-levels').to_bytes(2,'big')+b'marker-levels'+(4).to_bytes(2,'big')+(-1).to_bytes(4,'big',signed=True)
        numbers+=bytes([0x22])+len('printer-is-accepting-jobs').to_bytes(2,'big')+b'printer-is-accepting-jobs'+(1).to_bytes(2,'big')+b'\x00'
        numbers+=b'\x03'
        numbered=printing.parse_ipp(numbers)
        self.assertEqual(numbered['printer-state'],[4])
        self.assertEqual(numbered['marker-levels'],[-1])
        self.assertEqual(numbered['printer-is-accepting-jobs'],[False])
        self.assertEqual(printing.supply_percent(36,100),36)
        self.assertEqual(printing.supply_percent(20,200),10)
        self.assertEqual(printing.supply_percent(0,100),0)
        self.assertIsNone(printing.supply_percent(-1,100))
        self.assertIsNone(printing.supply_percent(-3,100))
        ipp=printing.readings_from_ipp({
            'printer-state':[5],
            'printer-state-reasons':['media-jam','none','toner-low'],
            'printer-is-accepting-jobs':[False],
            'marker-names':['Black Toner'],
            'marker-levels':[36,-1],
            'marker-high-levels':[100,100],
            'printer-firmware-string':['1.23'],
            'printer-device-id':['MFG:Brother;MDL:HL-L2380DW;SN:E12345;'],
        })
        self.assertEqual(ipp['state'],'Stopped')
        self.assertEqual(ipp['reasons'],['Paper jam','Toner low','Not accepting jobs'])
        self.assertEqual(ipp['supplies'],[{'name':'Black Toner','percent':36}])
        self.assertEqual(ipp['serial'],'E12345')
        self.assertEqual(ipp['firmware'],'1.23')
        snmp_lines=['8421','"BRN123"','4','Black Toner Cartridge','100','40','No Such Instance currently exists at this OID','No Such Instance currently exists at this OID','No Such Instance currently exists at this OID','Drum Unit','100','80']
        self.assertEqual(len(snmp_lines), len(printing.SNMP_OIDS))
        snmp=printing.readings_from_snmp(snmp_lines)
        self.assertEqual(snmp['pages'],8421)
        self.assertEqual(snmp['serial'],'BRN123')
        self.assertEqual(snmp['state'],'Printing')
        self.assertEqual(snmp['supplies'][0],{'name':'Black Toner Cartridge','percent':40})
        self.assertEqual(snmp['supplies'][1]['name'],'Drum Unit')
        self.assertEqual(snmp['supplies'][1]['percent'],80)
        with patch.object(printing,'ipp_attributes',return_value={}), patch.object(printing,'_snmp_lines',return_value=snmp_lines):
            live=printing.printer_stats('192.0.2.40')
        self.assertEqual(live['pages_label'],'8,421')
        self.assertEqual(live['supplies'][1]['name'],'Drum Unit')
        ready=printing.condition(live, online=True)
        self.assertEqual(ready['paper'],'ready')
        self.assertEqual(ready['jam'],'clear')
        self.assertEqual(ready['drum']['percent'],80)
        self.assertEqual(ready['toner']['name'],'Black Toner Cartridge')
        jammed=printing.condition({'reasons':['Paper jam','Out of paper'],'supplies':[{'name':'Black Ink','percent':12}]}, online=True)
        self.assertEqual(jammed['paper'],'empty')
        self.assertEqual(jammed['jam'],'jam')
        self.assertEqual(jammed['mark'],'Ink')
        self.assertEqual(printing.condition(None, online=False)['paper'],'unknown')
        self.assertEqual(printing.condition(None, online=False)['jam'],'unknown')
        with patch.object(printing,'ipp_attributes',return_value={}), patch.object(printing,'_snmp_lines',return_value=None):
            self.assertIsNone(printing.printer_stats('192.0.2.40'))
        listed='\n'.join([
            'PageSize/Media Size: Letter *A4',
            'brlaserEconomode/Toner save mode: *True False',
            'brlaserDensityAdjust/Toner Density: 94 100 *102 106',
            'Resolution/Resolution: 300dpi *600dpi 1200dpi',
        ])
        self.assertEqual(printing.density_options(listed),[
            ('brlaserEconomode','False'),
            ('brlaserDensityAdjust','106'),
        ])
        self.assertEqual(printing.density_options('PageSize/Media Size: *Letter'),[])

    def test_model_picks_a_toner_and_an_amazon_search(self):
        brother=supplies.replacement_for('Brother HL-L2380DW series')
        self.assertEqual(brother['kind'],'Toner')
        self.assertEqual(brother['name'],'TN660')
        self.assertIn('TN630', brother['note'])
        self.assertIn('https://www.amazon.com/s?k=TN660+toner+cartridge', brother['url'])
        self.assertIn('s=price-asc-rank', brother['url'])
        ink=supplies.replacement_for('HP OfficeJet Pro 9020')
        self.assertEqual(ink['kind'],'Ink')
        self.assertEqual(ink['name'],'')
        self.assertIn('OfficeJet+Pro+9020+ink+cartridge', ink['url'])
        self.assertIsNone(supplies.replacement_for('  '))
        with store.connection() as db:
            db.execute("INSERT OR IGNORE INTO printers(queue,label,host,protocol,identity) VALUES('TonerQ','Office Printer','192.0.2.41','socket','Brother HL-L2380DW series')")
        from fastapi.testclient import TestClient
        from app.main import app
        with patch.object(printing,'printer_reachable',return_value=False):
            with TestClient(app) as client:
                client.post('/login',data={'username':'admin','password':os.environ['ADMIN_PASSWORD']},follow_redirects=False)
                page=client.get('/?page=printers')
        self.assertIn('Toner TN660', page.text)
        self.assertIn('Lowest price on Amazon', page.text)
        self.assertIn('TN660+toner+cartridge', page.text)
        self.assertIn('rel="noopener noreferrer"', page.text)

    def test_overview_shows_toner_level(self):
        with store.connection() as db:
            db.execute("INSERT OR IGNORE INTO printers(queue,label,host,protocol,identity) VALUES('DashQ','Office Printer','192.0.2.43','socket','Brother HL-L2380DW series')")
        sample={
            'state':'Idle','reasons':['Toner low'],'pages':8421,'pages_label':'8,421',
            'supplies':[{'name':'Black Toner','percent':18},{'name':'Drum','percent':4}],
            'serial':'','firmware':'',
        }
        from fastapi.testclient import TestClient
        from app.main import app
        with patch.object(printing,'printer_reachable',return_value=True), patch.object(printing,'printer_stats',return_value=sample):
            with TestClient(app) as client:
                client.post('/login',data={'username':'admin','password':os.environ['ADMIN_PASSWORD']},follow_redirects=False)
                page=client.get('/')
        self.assertIn('Black Toner', page.text)
        self.assertIn('18%', page.text)
        self.assertIn('role="meter"', page.text)
        self.assertIn('aria-valuenow="18"', page.text)
        self.assertIn('width: 18%', page.text)
        self.assertIn('is-mid', page.text)
        self.assertIn('is-low', page.text)
        self.assertIn('8,421', page.text)
        self.assertIn('Toner low', page.text)
        self.assertIn('>Idle<', page.text)
        self.assertIn('TN660', page.text)
        self.assertIn('In the tray', page.text)
        self.assertIn('Clear', page.text)
        self.assertIn('Drum', page.text)

    def test_printer_page_shows_live_readings(self):
        with store.connection() as db:
            db.execute("INSERT OR IGNORE INTO printers(queue,label,host,protocol) VALUES('StatsQ','Office Printer','192.0.2.40','socket')")
        sample={
            'state':'Stopped','reasons':['Paper jam'],'pages':1200,'pages_label':'1,200',
            'supplies':[{'name':'Black Toner','percent':36}],'serial':'E12345','firmware':'1.23',
        }
        from fastapi.testclient import TestClient
        from app.main import app
        with patch.object(printing,'printer_reachable',return_value=True), patch.object(printing,'printer_stats',return_value=sample):
            with TestClient(app) as client:
                client.post('/login',data={'username':'admin','password':os.environ['ADMIN_PASSWORD']},follow_redirects=False)
                page=client.get('/?page=printers')
        self.assertIn('Paper jam', page.text)
        self.assertIn('Black Toner', page.text)
        self.assertIn('36%', page.text)
        self.assertIn('1,200', page.text)
        self.assertIn('E12345', page.text)
        self.assertIn('1.23', page.text)
        self.assertIn('>Stopped<', page.text)
        self.assertIn('Paper jam', page.text)
        self.assertIn('In the tray', page.text)
        self.assertIn('Not reported', page.text)

    def test_saved_printer_is_reread_and_matched(self):
        calls=[]
        def fake_cmd(*args, timeout=25):
            calls.append(args)
            stdout=''
            if args[0]=='lpinfo':
                stdout='\n'.join([
                    'drv:///brlaser.drv/brl2380d.ppd Brother HL-L2380DW series, using brlaser v6.2.8',
                    'everywhere IPP Everywhere',
                ])
            elif args[:2]==('lpoptions','-p'):
                stdout='brlaserEconomode/Toner save mode: *True False\nbrlaserDensityAdjust/Toner Density: 100 *102 106\n'
            return SimpleNamespace(returncode=0, stdout=stdout, stderr='')
        discovery={
            'reachable':True,'ipp':True,'socket':True,'make':'Brother',
            'model':'HL-L2380DW series','identity':'Brother HL-L2380DW series','device_id':'',
        }
        with store.connection() as db:
            db.execute("DELETE FROM printers WHERE queue='FadeFix'")
            db.execute("INSERT INTO printers(queue,label,host,protocol) VALUES('FadeFix','Office','192.168.1.20','ipp')")
        with patch.object(printing,'cmd',fake_cmd), patch.object(printing,'discover_printer',lambda host: discovery):
            printing.retarget_saved_queues()
        self.assertIn(('lpadmin','-p','FadeFix','-E','-v','socket://192.168.1.20:9100','-m','drv:///brlaser.drv/brl2380d.ppd'),calls)
        self.assertIn(('lpadmin','-p','FadeFix','-o','brlaserEconomode=True'),calls)
        self.assertIn(('lpadmin','-p','FadeFix','-o','brlaserDensityAdjust=102'),calls)
        self.assertNotIn(('lpadmin','-p','FadeFix','-o','brlaserDensityAdjust=106'),calls)
        joined=' '.join(' '.join(args) for args in calls)
        self.assertNotIn('-m everywhere', joined)
        with store.connection() as db:
            row=db.execute("SELECT protocol, identity, driver FROM printers WHERE queue='FadeFix'").fetchone()
            db.execute("DELETE FROM printers WHERE queue='FadeFix'")
        self.assertEqual(row['protocol'],'socket')
        self.assertEqual(row['identity'],'Brother HL-L2380DW series')
        self.assertEqual(row['driver'],'Brother HL-L2380DW series, using brlaser v6.2.8')

    def test_failed_retarget_keeps_the_saved_protocol(self):
        def fake_cmd(*args, timeout=25):
            return SimpleNamespace(returncode=1, stdout='', stderr='driver missing')
        with store.connection() as db:
            db.execute("DELETE FROM printers WHERE queue='Kept'")
            db.execute("INSERT INTO printers(queue,label,host,protocol) VALUES('Kept','Kept','192.168.1.21','ipp')")
        with patch.object(printing,'cmd',fake_cmd):
            printing.retarget_saved_queues()
        with store.connection() as db:
            row=db.execute("SELECT protocol FROM printers WHERE queue='Kept'").fetchone()
            db.execute("DELETE FROM printers WHERE queue='Kept'")
        self.assertEqual(row['protocol'],'ipp')

    def test_driver_settings_are_named_and_can_be_changed(self):
        listed='\n'.join([
            'brlaserEconomode/Toner save mode: *True False',
            'brlaserDensityAdjust/Toner Density: 94 100 *102 106',
            'PageSize/Media Size: Letter *A4',
        ])
        parsed=printing.parse_queue_options(listed)
        self.assertEqual(parsed[0]['key'],'brlaserEconomode')
        self.assertEqual(parsed[0]['current'],'True')
        self.assertEqual(parsed[1]['label'],'Toner Density')
        self.assertEqual([item['current'] for item in parsed if item['key']=='brlaserDensityAdjust'],['102'])
        calls=[]
        def fake_cmd(*args, timeout=25):
            calls.append(args)
            stdout=listed if args[:2]==('lpoptions','-p') else ''
            return SimpleNamespace(returncode=0, stdout=stdout, stderr='')
        discovery={
            'reachable':True,'ipp':True,'socket':True,'make':'Brother',
            'model':'HL-L2380DW series','identity':'Brother HL-L2380DW series','device_id':'',
        }
        drivers=[{
            'name':'drv:///brlaser.drv/brl2380d.ppd',
            'make-and-model':'Brother HL-L2380DW series, using brlaser v6.2.8',
            'device-id':'MFG:Brother;MDL:HL-L2380DW series;',
        }]
        with patch.object(printing,'cmd',fake_cmd):
            choice=printing.configure_queue('SetQ','192.0.2.44',discovery,drivers)
        self.assertEqual(choice['driver'],'Brother HL-L2380DW series, using brlaser v6.2.8')
        self.assertIn(('lpadmin','-p','SetQ','-o','brlaserEconomode=False'),calls)
        self.assertIn(('lpadmin','-p','SetQ','-o','brlaserDensityAdjust=106'),calls)
        with store.connection() as db:
            db.execute("DELETE FROM printers WHERE queue='SetQ'")
            db.execute("INSERT INTO printers(queue,label,host,protocol,identity,driver) VALUES('SetQ','Office Printer','192.0.2.44','socket','Brother HL-L2380DW series','Brother HL-L2380DW series, using brlaser v6.2.8')")
        sample={
            'state':'Idle','reasons':[],'pages':10,'pages_label':'10',
            'supplies':[{'name':'Black Toner','percent':50}],'serial':'','firmware':'',
        }
        from fastapi.testclient import TestClient
        from app.main import app
        with patch.object(printing,'printer_reachable',return_value=True), patch.object(printing,'printer_stats',return_value=sample), patch.object(printing,'cmd',fake_cmd), patch.object(printing,'cups_printers',return_value={'ok':True,'lines':[],'error':''}):
            with TestClient(app) as client:
                client.post('/login',data={'username':'admin','password':os.environ['ADMIN_PASSWORD']},follow_redirects=False)
                page=client.get('/?page=printers')
                self.assertIn('Brother HL-L2380DW series, using brlaser v6.2.8', page.text)
                self.assertIn('Driver settings', page.text)
                self.assertIn('name="opt_brlaserDensityAdjust"', page.text)
                self.assertIn('Toner save mode', page.text)
                token=re.search(r'name="token" value="([^"]+)"', page.text).group(1)
                calls.clear()
                saved=client.post('/printers/options', data={
                    'token':token,'queue':'SetQ',
                    'opt_brlaserEconomode':'False','opt_brlaserDensityAdjust':'100',
                }, follow_redirects=False)
        self.assertEqual(saved.status_code,303)
        self.assertIn(('lpadmin','-p','SetQ','-o','brlaserEconomode=False'),calls)
        self.assertIn(('lpadmin','-p','SetQ','-o','brlaserDensityAdjust=100'),calls)
        with store.connection() as db:
            db.execute("DELETE FROM printers WHERE queue='SetQ'")

    def test_printer_name_changes_without_rereading(self):
        with store.connection() as db:
            db.execute("DELETE FROM printers WHERE queue='EditMe'")
            db.execute("INSERT INTO printers(queue,label,host,protocol,identity,driver) VALUES('EditMe','Old','192.168.1.20','socket','Brother HL-L2380DW series','Brother driver')")
        calls=[]
        def fake_cmd(*args, timeout=25):
            calls.append(args)
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        with patch.object(printing,'cmd',fake_cmd):
            shown=printing.update_printer('EditMe','Front desk','192.168.1.20')
        self.assertEqual(shown,'Brother HL-L2380DW series')
        self.assertEqual(calls,[])
        with store.connection() as db:
            row=db.execute("SELECT label, host FROM printers WHERE queue='EditMe'").fetchone()
            db.execute("DELETE FROM printers WHERE queue='EditMe'")
        self.assertEqual(row['label'],'Front desk')
        self.assertEqual(row['host'],'192.168.1.20')

    def test_printer_address_change_selects_the_driver_again(self):
        calls=[]
        def fake_cmd(*args, timeout=25):
            calls.append(args)
            stdout=''
            if args[0]=='lpinfo':
                stdout='drv:///brlaser.drv/brl2380d.ppd Brother HL-L2380DW series, using brlaser v6.2.8\n'
            elif args[:2]==('lpoptions','-p'):
                stdout='brlaserDensityAdjust/Toner Density: 100 106\n'
            return SimpleNamespace(returncode=0, stdout=stdout, stderr='')
        discovery={'reachable':True,'ipp':True,'socket':True,'make':'Brother','model':'HL-L2380DW series','identity':'Brother HL-L2380DW series','device_id':''}
        with store.connection() as db:
            db.execute("DELETE FROM printers WHERE queue='EditMe'")
            db.execute("INSERT INTO printers(queue,label,host,protocol,identity,driver) VALUES('EditMe','Old','192.168.1.20','ipp','','')")
        with patch.object(printing,'cmd',fake_cmd), patch.object(printing,'discover_printer',lambda host: discovery):
            printing.update_printer('EditMe','Front desk','192.168.1.30')
        self.assertIn(('lpadmin','-p','EditMe','-E','-v','socket://192.168.1.30:9100','-m','drv:///brlaser.drv/brl2380d.ppd'),calls)
        with store.connection() as db:
            row=db.execute("SELECT label, host, protocol FROM printers WHERE queue='EditMe'").fetchone()
            db.execute("DELETE FROM printers WHERE queue='EditMe'")
        self.assertEqual((row['label'], row['host'], row['protocol']), ('Front desk','192.168.1.30','socket'))
        with self.assertRaises(printing.PrintError):
            printing.update_printer('Missing','Name','192.168.1.20')

    def test_dashboard_login(self):
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as client:
            self.assertEqual(client.get('/',follow_redirects=False).status_code,303)
            login=client.get('/login')
            self.assertEqual(login.status_code,200)
            self.assertIn('/static/favicon.svg', login.text)
            icon=client.get('/static/favicon.svg')
            self.assertEqual(icon.status_code,200)
            self.assertIn(b'<svg', icon.content)
            self.assertEqual(client.post('/login',data={'username':'admin','password':'wrong'},follow_redirects=False).status_code,303)
            good=client.post('/login',data={'username':'admin','password':os.environ['ADMIN_PASSWORD']},follow_redirects=False)
            self.assertEqual(good.status_code,303)
            page=client.get('/')
            self.assertEqual(page.status_code,200)
            self.assertIn('Printers, jobs, and intake status.',page.text)
            self.assertIn('Event log',page.text)
            self.assertIn('Sign out',page.text)
            for path, needle in (
                ('/?page=overview','Quick print'),
                ('/?page=printers','Add a printer'),
                ('/?page=jobs','Job log'),
                ('/?page=email','Allowed senders'),
                ('/?page=tailscale','Join this container to your tailnet'),
            ):
                body=client.get(path)
                self.assertEqual(body.status_code,200,path)
                self.assertIn(needle,body.text)
            token=re.search(r'name="token" value="([^"]+)"',page.text).group(1)
            bye=client.post('/logout',data={'token':token},follow_redirects=False)
            self.assertEqual(bye.status_code,303)
            self.assertIn('/login',bye.headers['location'])
            self.assertEqual(client.get('/',follow_redirects=False).status_code,303)

    def test_tailscale_notice_closes_until_the_next_sign_in(self):
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as client:
            denied=client.post('/tailscale/dismiss', data={'token':'x','page':'overview'}, follow_redirects=False)
            self.assertEqual(denied.status_code,303)
            self.assertIn('/login', denied.headers['location'])
            client.post('/login', data={'username':'admin','password':os.environ['ADMIN_PASSWORD']}, follow_redirects=False)
            first=client.get('/')
            self.assertIn('No tailnet address', first.text)
            self.assertIn('Dismiss Tailscale notice', first.text)
            token=re.search(r'name="token" value="([^"]+)"', first.text).group(1)
            closed=client.post('/tailscale/dismiss', data={'token':token,'page':'printers'}, follow_redirects=False)
            self.assertEqual(closed.status_code,303)
            self.assertIn('page=printers', closed.headers['location'])
            again=client.get('/?page=printers')
            self.assertNotIn('No tailnet address', again.text)
            self.assertNotIn('Dismiss Tailscale notice', again.text)
            other=client.get('/')
            self.assertNotIn('Dismiss Tailscale notice', other.text)
            signed=re.search(r'name="token" value="([^"]+)"', again.text).group(1)
            client.post('/logout', data={'token':signed}, follow_redirects=False)
            client.post('/login', data={'username':'admin','password':os.environ['ADMIN_PASSWORD']}, follow_redirects=False)
            fresh=client.get('/')
            self.assertIn('Dismiss Tailscale notice', fresh.text)
            online={'state':'Running','ip':'100.64.0.8','hostname':'printharbor','online':True,'installed':True,'tun':True,'detail':''}
            with patch('app.tailnet.status', return_value=online):
                connected=client.get('/')
            self.assertIn('100.64.0.8', connected.text)
            self.assertNotIn('Dismiss Tailscale notice', connected.text)

    def test_unauthenticated_actions_redirect(self):
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as client:
            response=client.post('/printers/add',data={'token':'x','label':'A','queue':'Queue','host':'127.0.0.1','protocol':'ipp'},follow_redirects=False)
            edit=client.post('/printers/edit',data={'token':'x','label':'A','queue':'Queue','host':'127.0.0.1','action':'save'},follow_redirects=False)
            self.assertEqual(edit.status_code,303)
            self.assertIn('/login',edit.headers['location'])
            self.assertEqual(response.status_code,303)
            self.assertIn('/login',response.headers['location'])

    def test_secret_roundtrip_and_legacy_double_encryption(self):
        store.set_setting('imap_password','app-password-value')
        store.set_setting('email_pin','abc123xyz')
        cfg=store.settings()
        self.assertEqual(cfg['imap_password'],'app-password-value')
        self.assertEqual(cfg['email_pin'],'abc123xyz')
        fernet=store._key()
        once=fernet.encrypt(b'legacy-pass').decode()
        twice=fernet.encrypt(once.encode()).decode()
        with store.connection() as db:
            db.execute("UPDATE settings SET value=? WHERE key='imap_password'",(twice,))
        self.assertEqual(store.settings()['imap_password'],'legacy-pass')

    def test_settings_save_is_atomic_for_invalid_pin(self):
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as client:
            client.post('/login',data={'username':'admin','password':os.environ['ADMIN_PASSWORD']},follow_redirects=False)
            page=client.get('/?page=email')
            token=re.search(r'name="token" value="([^"]+)"',page.text).group(1)
            before=store.settings()['poll_seconds']
            bad=client.post('/settings/save',data=self._settings(token,poll_seconds='123',new_pin='no'),follow_redirects=False)
            self.assertEqual(bad.status_code,303)
            self.assertIn('error=',bad.headers['location'])
            self.assertEqual(store.settings()['poll_seconds'],before)
            good=client.post('/settings/save',data=self._settings(token,poll_seconds='80',new_pin='abc12345',imap_password='abcd efgh ijkl',mail_enabled='1'),follow_redirects=True)
            self.assertEqual(good.status_code,200)
            cfg=store.settings()
            self.assertEqual(cfg['poll_seconds'],'80')
            self.assertEqual(cfg['email_pin'],'abc12345')
            self.assertEqual(cfg['imap_password'],'abcdefghijkl')
            self.assertEqual(cfg['mail_enabled'],'1')
            self.assertNotIn('abcdefghijkl',good.text)
            kept=client.post('/settings/save',data=self._settings(token,poll_seconds='90'),follow_redirects=False)
            self.assertEqual(kept.status_code,303)
            cfg=store.settings()
            self.assertEqual(cfg['imap_password'],'abcdefghijkl')
            self.assertEqual(cfg['email_pin'],'')
            self.assertEqual(cfg['poll_seconds'],'90')
            self.assertEqual(cfg['mail_enabled'],'0')

    def _settings(self, token, **overrides):
        data={
            'token':token,'imap_host':'imap.gmail.com','imap_port':'993','imap_user':'print@example.com',
            'imap_password':'','imap_folder':'INBOX','allowed_senders':'trusted@example.com','mail_recipient':'',
            'default_queue':'','default_sides':'one-sided','poll_seconds':'60','max_pages':'40','max_mb':'12',
            'max_files':'5','new_pin':'',
        }
        data.update(overrides)
        return data

    def test_failed_dedupe_is_not_a_success(self):
        with store.connection() as db:
            db.execute("INSERT OR IGNORE INTO printers(queue,label,host,protocol) VALUES('Dads_Brother','Dad','127.0.0.1','ipp')")
            db.execute("INSERT INTO jobs(dedupe,source,filename,queue,status,error) VALUES('dedupe-failed','email','note.pdf','Dads_Brother','error','conversion failed')")
        with patch.object(printing,'cmd') as fake:
            with self.assertRaises(printing.PrintError):
                printing.submit_file('note.pdf',b'%PDF', 'Dads_Brother', dedupe='dedupe-failed')
            fake.assert_not_called()

    def test_interrupted_jobs_are_marked_failed(self):
        with store.connection() as db:
            db.execute("INSERT INTO jobs(source,filename,queue,status) VALUES('upload','half.pdf','Dads_Brother','preparing')")
            job_id=db.execute("SELECT id FROM jobs WHERE filename='half.pdf' AND status='preparing'").fetchone()['id']
        store.initialize()
        with store.connection() as db:
            row=db.execute('SELECT status, error FROM jobs WHERE id=?',(job_id,)).fetchone()
        self.assertEqual(row['status'],'error')
        self.assertIn('Interrupted',row['error'])

    def _one_page_pdf(self, text):
        from reportlab.pdfgen import canvas
        import io
        buf=io.BytesIO()
        doc=canvas.Canvas(buf)
        doc.drawString(50,700,text)
        doc.showPage()
        doc.save()
        return buf.getvalue()

    def _park_pending(self):
        with store.connection() as db:
            db.execute("UPDATE jobs SET status='cancelled' WHERE status='pending'")

    def test_offline_printer_keeps_each_document_pending(self):
        self._park_pending()
        with store.connection() as db:
            db.execute("INSERT OR IGNORE INTO printers(queue,label,host,protocol) VALUES('WaitQ','Hold','192.0.2.8','socket')")
        with patch.object(printing,'printer_reachable',return_value=False), patch.object(printing,'cmd') as fake:
            first=printing.submit_file('first.pdf', self._one_page_pdf('one'), 'WaitQ')
            second=printing.submit_file('second.pdf', self._one_page_pdf('two'), 'WaitQ')
            fake.assert_not_called()
        with store.connection() as db:
            rows=db.execute("SELECT id,status,cups_id FROM jobs WHERE id IN (?,?) ORDER BY id",(first,second)).fetchall()
        self.assertEqual([row['status'] for row in rows], ['pending','pending'])
        self.assertEqual([row['cups_id'] for row in rows], ['',''])

    def test_queue_sends_in_order_after_the_printer_answers(self):
        self._park_pending()
        with store.connection() as db:
            db.execute("INSERT OR IGNORE INTO printers(queue,label,host,protocol) VALUES('OrderQ','Hold','192.0.2.9','socket')")
        sent=[]
        def fake_cmd(*args, **kwargs):
            self.assertEqual(args[0], 'lp')
            sent.append(args[2])
            return SimpleNamespace(returncode=0, stdout=f'request id is OrderQ-{len(sent)} (1 file(s))', stderr='')
        with patch.object(printing,'printer_reachable',return_value=False):
            first=printing.submit_file('order-one.pdf', self._one_page_pdf('one'), 'OrderQ', dedupe='order-first')
            second=printing.submit_file('order-two.pdf', self._one_page_pdf('two'), 'OrderQ', dedupe='order-second')
        with patch.object(printing,'printer_reachable',return_value=True), patch.object(printing,'cmd',side_effect=fake_cmd):
            printing.release_queues()
        self.assertEqual(sent, ['OrderQ','OrderQ'])
        with store.connection() as db:
            rows=db.execute("SELECT status,cups_id FROM jobs WHERE id IN (?,?) ORDER BY id",(first,second)).fetchall()
        self.assertEqual([row['status'] for row in rows], ['submitted','submitted'])
        self.assertEqual([row['cups_id'] for row in rows], ['OrderQ-1','OrderQ-2'])

    def test_queue_stops_when_the_next_document_finds_the_printer_offline(self):
        self._park_pending()
        with store.connection() as db:
            db.execute("INSERT OR IGNORE INTO printers(queue,label,host,protocol) VALUES('StopQ','Hold','192.0.2.10','socket')")
        answers=iter([True, False])
        with patch.object(printing,'printer_reachable',return_value=False):
            first=printing.submit_file('alive.pdf', self._one_page_pdf('one'), 'StopQ', dedupe='stop-alive')
            second=printing.submit_file('later.pdf', self._one_page_pdf('two'), 'StopQ', dedupe='stop-later')
        with patch.object(printing,'printer_reachable',side_effect=lambda *a, **k: next(answers)), patch.object(printing,'cmd',return_value=SimpleNamespace(returncode=0,stdout='request id is StopQ-7 (1 file(s))',stderr='')) as fake:
            printing.release_queues()
            self.assertEqual(fake.call_count, 1)
        with store.connection() as db:
            rows={row['id']: row['status'] for row in db.execute("SELECT id,status FROM jobs WHERE id IN (?,?)",(first,second))}
        self.assertEqual(rows[first], 'submitted')
        self.assertEqual(rows[second], 'pending')

    def test_sent_document_cannot_be_cancelled(self):
        self._park_pending()
        with store.connection() as db:
            db.execute("INSERT INTO jobs(source,filename,queue,status,cups_id) VALUES('upload','gone.pdf','OrderQ','submitted','OrderQ-9')")
            sent=db.execute("SELECT id FROM jobs WHERE filename='gone.pdf' AND cups_id='OrderQ-9'").fetchone()['id']
            db.execute("INSERT INTO jobs(source,filename,queue,status) VALUES('upload','waiting.pdf','OrderQ','pending')")
            waiting=db.execute("SELECT id FROM jobs WHERE filename='waiting.pdf' AND status='pending'").fetchone()['id']
        with self.assertRaises(printing.PrintError):
            printing.cancel_pending(sent)
        from fastapi.testclient import TestClient
        from app.main import app
        with patch.object(printing,'printer_reachable',return_value=False):
            with TestClient(app) as client:
                client.post('/login',data={'username':'admin','password':os.environ['ADMIN_PASSWORD']},follow_redirects=False)
                page=client.get('/?page=jobs')
                self.assertIn('Already sent', page.text)
                self.assertIn('Cancel waiting.pdf', page.text)
                self.assertNotIn('name="cups_id"', page.text)
                self.assertIn('>Pending<', page.text)
                self.assertIn('>Sent<', page.text)
                token=re.search(r'name="token" value="([^"]+)"', page.text).group(1)
                denied=client.post('/jobs/cancel',data={'token':token,'job_id':str(sent)},follow_redirects=False)
                self.assertIn('cannot+be+cancelled', denied.headers['location'])
                removed=client.post('/jobs/cancel',data={'token':token,'job_id':str(waiting)},follow_redirects=False)
                self.assertIn('Removed+from+the+queue', removed.headers['location'])
        with store.connection() as db:
            self.assertEqual(db.execute('SELECT status FROM jobs WHERE id=?',(sent,)).fetchone()['status'], 'submitted')
            self.assertEqual(db.execute('SELECT status FROM jobs WHERE id=?',(waiting,)).fetchone()['status'], 'cancelled')

    def test_safe_filename(self):
        self.assertEqual(printing._safe_filename('../../etc/passwd.pdf'),'passwd.pdf')
        long_name=printing._safe_filename(('a'*200)+'.pdf')
        self.assertTrue(long_name.endswith('.pdf'))
        self.assertLessEqual(len(long_name),180)

    def test_missing_cups_command_is_explicit(self):
        with patch.object(printing.subprocess, 'run', side_effect=FileNotFoundError(2, 'The system cannot find the file specified')):
            report=printing.active_jobs()
        self.assertFalse(report['ok'])
        self.assertIn('lpstat', report['error'])
        self.assertNotIn('WinError', report['error'])

    def test_lpstat_empty_is_not_a_failure(self):
        empty=printing.interpret_lpstat(SimpleNamespace(returncode=1,stdout='',stderr='lpstat: No destinations added.\n'))
        self.assertTrue(empty['ok'])
        self.assertEqual(empty['lines'],[])
        down=printing.interpret_lpstat(SimpleNamespace(returncode=1,stdout='',stderr='lpstat: Unable to connect to server\n'))
        self.assertFalse(down['ok'])
        listed=printing.interpret_lpstat(SimpleNamespace(returncode=0,stdout='printer Office_Brother is idle.\n',stderr=''))
        self.assertEqual(listed['lines'],['printer Office_Brother is idle.'])

    def test_multipage_image_and_png(self):
        from PIL import Image
        import io
        frames=[Image.new('RGB',(30,30),color) for color in ('red','blue','green')]
        buf=io.BytesIO()
        frames[0].save(buf,format='TIFF',save_all=True,append_images=frames[1:])
        path,pages=printing.prepare_bytes('scan.tiff',buf.getvalue(),5)
        self.assertEqual(pages,3)
        Path(path).unlink(missing_ok=True)
        with self.assertRaises(printing.PrintError):
            printing.prepare_bytes('scan.tiff',buf.getvalue(),2)
        png=io.BytesIO()
        Image.new('RGB',(25,25),'blue').save(png,format='PNG')
        path,pages=printing.prepare_bytes('photo.png',png.getvalue(),5)
        self.assertEqual(pages,1)
        Path(path).unlink(missing_ok=True)

    def test_default_sides_and_poll_summary(self):
        msg=EmailMessage()
        msg['From']='trusted@example.com'
        msg['To']='print@example.com'
        msg['Subject']='PRINT PIN:abcdef12'
        cfg={'allowed_senders':'trusted@example.com','mail_recipient':'','email_pin':'abcdef12','default_sides':'two-sided-long-edge'}
        ok,_,command,_=mailer.allowed_message(msg,cfg)
        self.assertTrue(ok)
        self.assertEqual(command['sides'],'two-sided-long-edge')
        cfg['default_sides']='nope'
        self.assertEqual(mailer.allowed_message(msg,cfg)[2]['sides'],'one-sided')
        self.assertEqual(mailer.summarize_poll(0,0)[0],'Watching')
        self.assertEqual(mailer.summarize_poll(1,0)[0],'Healthy')
        self.assertEqual(mailer.summarize_poll(2,1)[0],'Attention')

    def test_blank_allowlist_accepts_any_sender(self):
        msg=EmailMessage()
        msg['From']='stranger@example.com'
        msg['To']='print@example.com'
        msg['Subject']='PRINT PIN:abcdef12'
        open_list={'allowed_senders':'','mail_recipient':'','email_pin':'abcdef12','default_sides':'one-sided'}
        self.assertTrue(mailer.allowed_message(msg, open_list)[0])
        closed=dict(open_list, allowed_senders='trusted@example.com')
        self.assertFalse(mailer.allowed_message(msg, closed)[0])
        closed['mail_recipient']='print@example.com'
        msg.replace_header('From','trusted@example.com')
        self.assertTrue(mailer.allowed_message(msg, closed)[0])
        msg.replace_header('To','other@example.com')
        self.assertFalse(mailer.allowed_message(msg, closed)[0])
        open_mail=EmailMessage()
        open_mail['From']='person@example.com'
        open_mail['To']='other@example.com'
        open_mail['Subject']='Please PRINT DUPLEX'
        open_cfg={'allowed_senders':'','mail_recipient':'','email_pin':'','default_sides':'one-sided'}
        ok, reason, command, _=mailer.allowed_message(open_mail, open_cfg)
        self.assertTrue(ok, reason)
        self.assertEqual(command, {'sides':'two-sided-long-edge','copies':1})

    def test_disabled_badge_points_at_this_page(self):
        store.set_setting('mail_enabled','0')
        mailer.check_once()
        self.assertEqual(mailer.snapshot()['detail'], 'Turn on email printing, then save.')
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as client:
            client.post('/login',data={'username':'admin','password':os.environ['ADMIN_PASSWORD']},follow_redirects=False)
            page=client.get('/?page=email')
            self.assertIn('Turn on email printing, then save.', page.text)
            self.assertIn('Enable email printing', page.text)
            self.assertIn('Blank accepts any sender', page.text)
            self.assertIn('Blank accepts any To or Cc', page.text)
            self.assertIn('Blank means no PIN', page.text)
            self.assertIn('Mail already in the folder is left alone', page.text)
            self.assertNotIn('Event log', page.text)
            self.assertNotIn('What is enforced', page.text)
            self.assertLess(page.text.find('Enable email printing'), page.text.find('IMAP server'))

    def test_folder_decoding_and_list_does_not_save(self):
        self.assertEqual(mailer.decode_imap_utf7('INBOX'), 'INBOX')
        self.assertEqual(mailer.decode_imap_utf7('A&-B'), 'A&B')
        self.assertEqual(mailer.decode_imap_utf7('&AOk-'), '\u00e9')
        self.assertEqual(mailer.mailbox_name(b'(\\HasNoChildren) "/" "INBOX"'), 'INBOX')
        self.assertEqual(mailer.mailbox_name(b'(\\HasNoChildren) "/" "&AOk-"'), '\u00e9')
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as client:
            client.post('/login',data={'username':'admin','password':os.environ['ADMIN_PASSWORD']},follow_redirects=False)
            before=store.settings()['poll_seconds']
            page=client.get('/?page=email')
            token=re.search(r'name="token" value="([^"]+)"', page.text).group(1)
            with patch('app.mailer.list_folders', return_value=['INBOX', 'Print']):
                loaded=client.post('/mail/folders', data=self._settings(token, poll_seconds='333', imap_user='print@example.com', imap_password='app-pass-value'))
            self.assertEqual(loaded.status_code, 200)
            self.assertIn('Folders loaded', loaded.text)
            self.assertIn('>Print<', loaded.text)
            self.assertNotIn('app-pass-value', loaded.text)
            self.assertEqual(store.settings()['poll_seconds'], before)

    def test_result_email_uses_alias_from(self):
        msg=EmailMessage()
        msg['From']='Person <person@example.com>'
        msg['Subject']='PRINT PIN:abcdef12'
        msg['Message-ID']='<job@example.com>'
        config={
            'reply_enabled':'1','smtp_from':'printme@example.com','imap_user':'devon@example.com',
            'imap_password':'secret-pass','smtp_host':'smtp.gmail.com','smtp_port':'587',
        }
        result={'accepted':True,'jobs':2,'errors':0,'reason':'','queue':'Office','notes':['a.pdf accepted by CUPS for Office']}

        class FakeSMTP:
            def __init__(self, host, port, timeout=None):
                self.host=host
                self.port=port
                self.messages=[]
                FakeSMTP.last=self
            def ehlo(self):
                return None
            def starttls(self):
                self.tls=True
            def login(self, user, password):
                self.login_args=(user, password)
            def send_message(self, message):
                self.messages.append(message)
            def quit(self):
                return None

        with patch('app.mailer.smtplib.SMTP', FakeSMTP):
            mailer.notify_sender(msg, config, result)
        sent=FakeSMTP.last.messages[0]
        self.assertEqual(FakeSMTP.last.login_args, ('devon@example.com','secret-pass'))
        self.assertTrue(FakeSMTP.last.tls)
        self.assertEqual(sent['From'], 'printme@example.com')
        self.assertEqual(sent['To'], 'person@example.com')
        self.assertEqual(sent['Subject'], 'Re: PRINT PIN:abcdef12')
        self.assertEqual(sent['In-Reply-To'], '<job@example.com>')
        plain = sent.get_body(preferencelist=('plain',)).get_content()
        html = sent.get_body(preferencelist=('html',)).get_content()
        self.assertIn("It's on the way.", plain)
        self.assertIn('a.pdf', plain)
        self.assertIn('Printed', plain)
        self.assertNotIn('CUPS', plain)
        self.assertIn('PRINT CONFIRMED', html)
        self.assertIn('a.pdf', html)
        self.assertIn('text-align:center', html)
        self.assertNotIn('CUPS', html)
        self.assertNotIn('PIN', html)
        self.assertNotIn('folder', html.lower())
        self.assertNotIn('secret-pass', plain)
        self.assertNotIn('secret-pass', html)
        hostile = mailer.render_reply({'accepted': False, 'jobs': 0, 'errors': 0, 'reason': '<b>nope</b>', 'queue': 'Office', 'notes': [], 'files': []})
        self.assertIn('NOT PRINTED', hostile)
        self.assertIn('&lt;b&gt;nope&lt;/b&gt;', hostile)
        self.assertNotIn('<b>nope</b>', hostile)
        friendly = mailer.render_reply({'accepted': False, 'jobs': 0, 'errors': 0, 'reason': 'Sender is not allowlisted', 'queue': 'Office_Printer', 'notes': [], 'files': []})
        self.assertIn('approved to print', friendly)
        self.assertNotIn('allowlisted', friendly)
        self.assertNotIn('CUPS', friendly)
        config['reply_enabled']='0'
        with patch('app.mailer.smtplib.SMTP') as smtp:
            mailer.notify_sender(msg, config, result)
            smtp.assert_not_called()
        config['reply_enabled']='1'
        config['smtp_port']='465'
        with patch('app.mailer.smtplib.SMTP_SSL', FakeSMTP), patch('app.mailer.smtplib.SMTP') as plain:
            mailer.notify_sender(msg, config, result)
            plain.assert_not_called()
        self.assertEqual(FakeSMTP.last.port, 465)

    def test_save_keeps_password_and_stores_from_address(self):
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as client:
            client.post('/login',data={'username':'admin','password':os.environ['ADMIN_PASSWORD']},follow_redirects=False)
            page=client.get('/?page=email')
            token=re.search(r'name="token" value="([^"]+)"', page.text).group(1)
            client.post('/settings/save', data=self._settings(token, imap_password='kept-password', smtp_from='printme@example.com', reply_enabled='1', allowed_senders=''), follow_redirects=False)
            kept=client.post('/settings/save', data=self._settings(token, smtp_from='printme@example.com', reply_enabled='1', allowed_senders=''), follow_redirects=False)
            self.assertEqual(kept.status_code, 303)
            cfg=store.settings()
            self.assertEqual(cfg['imap_password'], 'kept-password')
            self.assertEqual(cfg['smtp_from'], 'printme@example.com')
            self.assertEqual(cfg['smtp_host'], 'smtp.gmail.com')
            self.assertEqual(cfg['smtp_port'], '587')
            self.assertEqual(cfg['allowed_senders'], '')
            self.assertEqual(cfg['reply_enabled'], '1')

    def test_tailscale_rejects_a_bad_key_before_calling_the_client(self):
        with self.assertRaises(tailnet.TailscaleError):
            tailnet.connect('not-a-key', 'printharbor')
        with patch('app.tailnet.shutil.which', return_value='/usr/bin/tailscale'), patch('app.tailnet.Path.exists', return_value=True), patch('app.tailnet._run') as run:
            with self.assertRaises(tailnet.TailscaleError):
                tailnet.connect('tskey-auth-' + ('a' * 24), 'Bad Host')
            run.assert_not_called()
            key = 'tskey-auth-' + ('b' * 24)
            run.return_value = SimpleNamespace(returncode=1, stdout='', stderr='rejected ' + key)
            with self.assertRaises(tailnet.TailscaleError) as caught:
                tailnet.connect(key, 'printharbor')
            self.assertNotIn(key, str(caught.exception))

    def test_tailscale_form_and_unauthenticated_join(self):
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as client:
            denied = client.post('/tailscale/up', data={'token': 'x', 'auth_key': 'tskey-auth-secret', 'hostname': 'printharbor'}, follow_redirects=False)
            self.assertEqual(denied.status_code, 303)
            self.assertIn('/login', denied.headers['location'])
            client.post('/login', data={'username': 'admin', 'password': os.environ['ADMIN_PASSWORD']}, follow_redirects=False)
            ready = {
                'state': 'NeedsLogin', 'ip': '', 'hostname': '', 'online': False,
                'installed': True, 'tun': True, 'detail': 'Paste an auth key to join the tailnet.',
            }
            with patch('app.tailnet.status', return_value=ready):
                page = client.get('/?page=tailscale')
                self.assertIn('Auth key', page.text)
                self.assertNotIn('type="password" value=', page.text)
                token = re.search(r'name="token" value="([^"]+)"', page.text).group(1)
            bad = client.post('/tailscale/up', data={'token': token, 'auth_key': 'not-a-key', 'hostname': 'printharbor'}, follow_redirects=False)
            self.assertIn('error=', bad.headers['location'])
            self.assertNotIn('not-a-key', bad.headers['location'])

    def test_folder_watch_ignores_mail_already_present(self):
        self.assertEqual(mailer.uids_after(b'4 5 9', 5), [b'9'])
        self.assertEqual(mailer.uids_after(b'', 0), [])
        self.assertEqual(mailer.parse_status(b'INBOX (UIDNEXT 12 UIDVALIDITY 4)'), (4, 12))
        self.assertEqual(mailer.fallback_wait({'poll_seconds': '60'}), 5)
        self.assertEqual(mailer.fallback_wait({'poll_seconds': '2'}), 2)
        cfg={'imap_host':'imap.gmail.com','imap_user':'print@example.com','imap_folder':'Print'}
        last, primed=mailer.adopt_cursor(cfg, 4, 12)
        self.assertTrue(primed)
        self.assertEqual(last, 11)
        saved=store.settings()
        again=dict(cfg, mail_watch=saved['mail_watch'], mail_uidvalidity=saved['mail_uidvalidity'], mail_last_uid=saved['mail_last_uid'])
        last, primed=mailer.adopt_cursor(again, 4, 20)
        self.assertFalse(primed)
        self.assertEqual(last, 11)
        last, primed=mailer.adopt_cursor(again, 9, 3)
        self.assertTrue(primed)
        self.assertEqual(last, 2)

    def test_idle_returns_when_the_folder_changes(self):
        lines=[b'+ idling\r\n', b'* 3 EXISTS\r\n', b'T1 OK IDLE terminated\r\n']

        class Sock:
            def settimeout(self, value):
                pass

        class Stub:
            def __init__(self):
                self.sock=Sock()
                self.sent=[]
            def _new_tag(self):
                return b'T1'
            def send(self, data):
                self.sent.append(data)
            def readline(self):
                return lines.pop(0)

        conn=Stub()
        mailer.STOP.clear()
        try:
            with patch('app.mailer.select.select', return_value=([True], [], [])):
                self.assertTrue(mailer.wait_for_new_mail(conn, seconds=5))
        finally:
            mailer.STOP.set()
        self.assertTrue(any(item.endswith(b'IDLE\r\n') for item in conn.sent))
        self.assertIn(b'DONE\r\n', conn.sent)

    def _png(self):
        from io import BytesIO
        from PIL import Image
        buf=BytesIO()
        Image.new('RGB',(8,8),(20,40,60)).save(buf,'PNG')
        return buf.getvalue()

    def _reset_images(self):
        images._index['mtime']=None
        images._index['entries']=[]
        path=images.fingerprint_file()
        if path.exists():
            path.unlink()
        with store.connection() as db:
            db.execute('DELETE FROM image_catalog')

    def _enable_images(self):
        os.environ['PRINTHARBOR_IMAGE_NETWORK']='1'
        images.STOP.set()

    def _disable_images(self):
        os.environ['PRINTHARBOR_IMAGE_NETWORK']='0'
        images.STOP.set()

    def test_model_names_normalize_and_do_not_cross_nearby_codes(self):
        brother=[images.identify(text)['code'] for text in (
            'Brother HL-L2350DW series','Brother HL L2350DW','HL-L2350DW','Brother Industries HL-L2350DW',
        )]
        self.assertEqual(brother, ['hll2350dw']*4)
        self.assertNotEqual(images.identify('HL-L2370DW')['code'], 'hll2350dw')
        hp=images.identify('HP LaserJet Pro M404dn')
        self.assertEqual(hp['manufacturer'], 'hp')
        self.assertEqual(hp['code'], images.identify('Hewlett-Packard LaserJet Pro M404dn')['code'])
        self.assertEqual(hp['code'], 'm404dn')
        self.assertEqual(hp['category'], 'mono')
        epson=images.identify('Epson EcoTank ET-2800')
        self.assertEqual(epson['code'], images.identify('Epson ET 2800')['code'])
        self.assertEqual(epson['code'], 'et2800')
        self.assertEqual(epson['category'], 'inkjet')
        canon=images.identify('Canon PIXMA TS3522')
        self.assertEqual(canon['code'], images.identify('Canon TS-3522')['code'])
        self.assertEqual(canon['code'], 'ts3522')
        self.assertEqual(canon['category'], 'inkjet')
        entries=[
            {'id':'3952','name':'Brother HL-L2350DW'},
            {'id':'3953','name':'Brother HL-L2370DW'},
        ]
        found=images.match_entries(entries, images.identify('Brother HL-L2350DW series'))
        self.assertEqual(found['match']['id'], '3952')
        self.assertGreaterEqual(found['confidence'], 0.9)
        other=images.match_entries(entries, images.identify('HL-L2370DW'))
        self.assertEqual(other['match']['id'], '3953')
        self.assertFalse(any(item['id']=='3952' for item in other['candidates']))
        near=images.match_entries(
            [{'id':'1','name':'HP LaserJet Pro M404n'},{'id':'2','name':'HP LaserJet Pro M404dn'}],
            images.identify('HP LaserJet Pro M404'),
        )
        self.assertIsNone(near['match'])
        self.assertGreaterEqual(len(near['candidates']), 1)

    def test_image_providers_cache_fallbacks_and_overrides(self):
        self._reset_images()
        self._enable_images()
        png=self._png()
        calls=[]

        def fake(url, timeout=8, limit=2_000_000):
            calls.append(url)
            if 'fingerprint-database.json' in url:
                return json.dumps({
                    '3952':'Brother HL-L2350DW',
                    '3953':'Brother HL-L2370DW',
                }).encode(), 'application/json'
            if 'static.ui.com' in url and '3952' in url:
                return png, 'image/png'
            if 'commons.wikimedia.org' in url:
                return json.dumps({'query':{'pages':{}}}).encode(), 'application/json'
            raise images.ImageError('unexpected address')

        try:
            with patch('app.images.fetch', side_effect=fake):
                first=images.resolve_model(images.identify('Brother HL-L2350DW series'), force=True)
                self.assertEqual(first['source'], 'unifi')
                self.assertEqual(first['fingerprint_id'], '3952')
                self.assertGreaterEqual(first['confidence'], 0.9)
                self.assertTrue(Path(first['local_path']).is_file())
                used=len(calls)
                second=images.resolve_model(images.identify('HL-L2350DW'))
                self.assertEqual(len(calls), used)
                self.assertEqual(second['local_path'], first['local_path'])
                with store.connection() as db:
                    db.execute("INSERT INTO printers(queue,label,host,protocol,identity) VALUES('ImgA','A','192.0.2.50','socket','Brother HL-L2350DW series')")
                    db.execute("INSERT INTO printers(queue,label,host,protocol,identity) VALUES('ImgB','B','192.0.2.51','socket','Brother HL-L2350DW')")
                images.resolve_printer('ImgA')
                after=len(calls)
                images.resolve_printer('ImgB')
                self.assertEqual(len(calls), after)

                self._reset_images()
                calls.clear()
                unknown=images.resolve_model(images.identify('Mystery ZX9000'), force=True)
                self.assertEqual(unknown['source'], 'miss')
                self.assertEqual(unknown['category'], 'generic')
                self.assertTrue(unknown['negative_until'])
                quiet=len(calls)
                again=images.resolve_model(images.identify('Mystery ZX9000'))
                self.assertEqual(len(calls), quiet)
                self.assertEqual(again['source'], 'miss')
                with store.connection() as db:
                    db.execute("INSERT INTO printers(queue,label,host,protocol,identity) VALUES('ImgU','U','192.0.2.52','socket','Mystery ZX9000')")
                path, media=images.file_for('ImgU')
                self.assertEqual(media, 'image/svg+xml')
                self.assertTrue(str(path).endswith('generic.svg'))

                self._reset_images()
                calls.clear()

                def broken(url, timeout=8, limit=2_000_000):
                    calls.append(url)
                    if 'fingerprint-database.json' in url:
                        return json.dumps({'3952':'Brother HL-L2350DW'}).encode(), 'application/json'
                    if 'static.ui.com' in url:
                        return b'<html>nope</html>', 'text/html'
                    return json.dumps({'query':{'pages':{}}}).encode(), 'application/json'

                with patch('app.images.fetch', side_effect=broken):
                    rejected=images.resolve_model(images.identify('Brother HL-L2350DW series'), force=True)
                self.assertEqual(rejected['source'], 'miss')
                self.assertFalse(rejected['local_path'])
                self.assertTrue(any('static.ui.com' in url for url in calls))
                self.assertTrue(any('commons.wikimedia.org' in url for url in calls))

                self._reset_images()
                calls.clear()

                def slow(url, timeout=8, limit=2_000_000):
                    calls.append(url)
                    if 'fingerprint-database.json' in url:
                        return json.dumps({'3952':'Brother HL-L2350DW'}).encode(), 'application/json'
                    raise TimeoutError('slow')

                with patch('app.images.fetch', side_effect=slow):
                    timed=images.resolve_model(images.identify('Brother HL-L2350DW series'), force=True)
                self.assertEqual(timed['source'], 'miss')
                self.assertTrue(any('static.ui.com' in url for url in calls))
                self.assertTrue(any('commons.wikimedia.org' in url for url in calls))
                with store.connection() as db:
                    db.execute("INSERT INTO printers(queue,label,host,protocol,identity) VALUES('ImgOff','Off','192.0.2.53','socket','Brother HL-L2350DW series')")
                offline, offline_type=images.file_for('ImgOff')
                self.assertEqual(offline_type, 'image/svg+xml')
                self.assertTrue(offline.is_file())

                kept=images._cache_file('brother|hll2350dw')
                kept.write_bytes(png)
                images._save_catalog({
                    'model_key':'brother|hll2350dw','manufacturer':'brother','model':'hll2350dw','source':'unifi',
                    'fingerprint_id':'3952','local_path':str(kept),'confidence':1,'license':'','attribution':'Ubiquiti',
                    'source_page':'','checked_at':'','negative_until':'','category':'mono','candidates':'[]',
                })
                before=kept.read_bytes()
                with patch('app.images.fetch', side_effect=TimeoutError('down')):
                    held=images.resolve_model(images.identify('Brother HL-L2350DW series'), force=True)
                self.assertEqual(held['local_path'], str(kept))
                self.assertEqual(kept.read_bytes(), before)

                self._reset_images()
                pages=[
                    {'title':'File:Brother logo.jpg','license':'CC BY 4.0','url':'https://upload.wikimedia.org/logo.jpg','artist':'','page':'','mime':'image/jpeg'},
                    {'title':'File:Brother HL-L2370DW.jpg','license':'CC BY 4.0','url':'https://upload.wikimedia.org/near.jpg','artist':'','page':'','mime':'image/jpeg'},
                    {'title':'File:Brother HL-L2350DW screenshot.jpg','license':'CC BY 4.0','url':'https://upload.wikimedia.org/shot.jpg','artist':'','page':'','mime':'image/jpeg'},
                    {'title':'File:Brother HL-L2350DW.jpg','license':'CC BY-NC 4.0','url':'https://upload.wikimedia.org/nc.jpg','artist':'','page':'','mime':'image/jpeg'},
                    {'title':'File:Brother HL-L2350DW front.jpg','license':'CC BY-SA 4.0','url':'https://upload.wikimedia.org/good.jpg','artist':'Ada','page':'https://commons.wikimedia.org/wiki/File:Brother_HL-L2350DW_front.jpg','mime':'image/jpeg'},
                ]
                self.assertIsNone(images.pick_wiki(pages[:4], 'hll2350dw'))
                self.assertEqual(images.pick_wiki(pages, 'hll2350dw')['title'], 'File:Brother HL-L2350DW front.jpg')
                self.assertFalse(images.license_ok('CC BY-ND 2.0'))
                self.assertTrue(images.license_ok('Public domain'))

                def commons(url, timeout=8, limit=2_000_000):
                    if 'fingerprint-database.json' in url:
                        return b'{"1":"Unrelated QQ11"}', 'application/json'
                    if 'commons.wikimedia.org/w/api.php' in url:
                        return json.dumps({'query':{'pages':{'1':{
                            'title':'File:Brother HL-L2350DW front.jpg',
                            'imageinfo':[{
                                'thumburl':'https://upload.wikimedia.org/good.jpg',
                                'descriptionurl':'https://commons.wikimedia.org/wiki/File:Brother_HL-L2350DW_front.jpg',
                                'mime':'image/jpeg',
                                'extmetadata':{
                                    'LicenseShortName':{'value':'CC BY-SA 4.0'},
                                    'Artist':{'value':'<a href="https://example.com">Ada</a>'},
                                },
                            }],
                        }}}}).encode(), 'application/json'
                    if url=='https://upload.wikimedia.org/good.jpg':
                        return png, 'image/jpeg'
                    return b'<html>rejected</html>', 'text/html'

                with patch('app.images.fetch', side_effect=commons):
                    wiki=images.resolve_model(images.identify('Brother HL-L2350DW series'), force=True)
                self.assertEqual(wiki['source'], 'wikimedia')
                self.assertEqual(wiki['license'], 'CC BY-SA 4.0')
                self.assertEqual(wiki['attribution'], 'Ada')
                self.assertIn('commons.wikimedia.org', wiki['source_page'])

                catalog=images.fingerprint_file()
                catalog.write_bytes(b'{"3952":"Brother HL-L2350DW"}')
                with patch('app.images.fetch', side_effect=TimeoutError('refresh failed')):
                    self.assertTrue(images.refresh_fingerprint(force=True))
                self.assertEqual(catalog.read_bytes(), b'{"3952":"Brother HL-L2350DW"}')

                with store.connection() as db:
                    db.execute("INSERT INTO printers(queue,label,host,protocol,identity) VALUES('ImgM','Mine','192.0.2.54','socket','Brother HL-L2350DW series')")
                images.save_upload('ImgM','mine.png',png)
                images.apply_preview('ImgM')
                saved=Path(images._printer('ImgM')['image_path'])
                original=saved.read_bytes()
                with patch('app.images.fetch', side_effect=images.ImageError('down')):
                    images.rescan_all()
                row=images._printer('ImgM')
                self.assertEqual(row['image_mode'], 'manual')
                self.assertEqual(Path(row['image_path']).read_bytes(), original)
                with self.assertRaises(images.ImageError):
                    images.save_upload('ImgM','bad.svg',b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>')
                with self.assertRaises(images.ImageError):
                    images.save_upload('ImgM','bad.png',b'not an image')
                with patch('app.images.urlopen', side_effect=AssertionError('network')):
                    with self.assertRaises(images.ImageError):
                        images.fetch('http://static.ui.com/fingerprint/0/1_257x257.png')
                    with self.assertRaises(images.ImageError):
                        images.fetch('https://example.com/printer.png')
        finally:
            self._disable_images()

    def test_printer_image_page_uses_a_local_picture(self):
        self._reset_images()
        with store.connection() as db:
            db.execute("INSERT OR IGNORE INTO printers(queue,label,host,protocol,identity) VALUES('ImgPage','Office Printer','192.0.2.55','socket','Brother HL-L2350DW series')")
        from fastapi.testclient import TestClient
        from app.main import app
        with patch.object(printing,'printer_reachable',return_value=False), patch.object(printing,'cups_printers',return_value={'ok':True,'lines':[],'error':''}):
            with TestClient(app) as client:
                blocked=client.get('/printer-images/ImgPage', follow_redirects=False)
                self.assertEqual(blocked.status_code, 303)
                client.post('/login',data={'username':'admin','password':os.environ['ADMIN_PASSWORD']},follow_redirects=False)
                page=client.get('/?page=printers')
                self.assertEqual(page.status_code, 200)
                self.assertIn('printer-photo', page.text)
                self.assertIn('Change printer image', page.text)
                self.assertNotIn('Identify printer images automatically', page.text)
                self.assertNotIn('Use the UniFi device catalog', page.text)
                self.assertNotIn('printer-figure', page.text)
                picture=client.get('/printer-images/ImgPage')
                self.assertEqual(picture.status_code, 200)
                self.assertEqual(picture.headers['content-type'].split(';',1)[0], 'image/svg+xml')
                self.assertIn(b'<svg', picture.content)

    def test_supply_faults_reject_the_document(self):
        fine={'reasons':[],'supplies':[{'name':'Black Toner','percent':11},{'name':'Waste Toner','percent':0}]}
        self.assertIsNone(printing.supply_problem(fine))
        low=printing.supply_problem({'reasons':[],'supplies':[{'name':'Black Toner','percent':8},{'name':'Drum Unit','percent':10}]})
        self.assertIn('Black Toner is at 8%', low['reason'])
        self.assertIn('Drum Unit is at 10%', low['reason'])
        self.assertIn('not printed', low['reason'])
        self.assertTrue(low['notice'])
        empty=printing.supply_problem({'reasons':['Out of paper'],'supplies':[{'name':'Black Toner','percent':40}]})
        self.assertIn('out of paper', empty['reason'])
        self.assertFalse(empty['notice'])
        jammed=printing.supply_problem({'reasons':['Paper jam'],'supplies':[{'name':'Cyan Ink','percent':4}]})
        self.assertIn('paper jam', jammed['reason'])
        self.assertFalse(jammed['notice'])
        self.assertIn('Cyan Ink is at 4%', jammed['reason'])

        with store.connection() as db:
            db.execute("INSERT OR REPLACE INTO printers(queue,label,host,protocol,identity) VALUES('LowQ','Office Printer','192.0.2.70','socket','Brother HL-L2380DW series')")
        printing._NOTICE_AT.pop('LowQ', None)
        calls=[]

        def fake_cmd(*args, **kwargs):
            calls.append(args)
            return SimpleNamespace(returncode=0, stdout='request id is LowQ-1 (1 file(s))', stderr='')

        stats={'state':'Idle','reasons':[],'pages':None,'supplies':[{'name':'Black Toner','percent':8}],'serial':'','firmware':''}
        with patch.object(printing,'printer_reachable',return_value=True), patch.object(printing,'printer_stats',return_value=stats), patch.object(printing,'cmd',side_effect=fake_cmd):
            with self.assertRaises(printing.PrintError) as caught:
                printing.submit_file('note.pdf', b'%PDF-1.4', 'LowQ', source='email', sender='person@example.com', dedupe='low-1')
            with self.assertRaises(printing.PrintError):
                printing.submit_file('again.pdf', b'%PDF-1.4', 'LowQ', source='email', sender='person@example.com', dedupe='low-2')
        self.assertIn('8%', str(caught.exception))
        self.assertEqual(len(calls), 1)
        self.assertTrue(str(calls[0][-1]).find('notice-') >= 0)
        with store.connection() as db:
            row=db.execute("SELECT status, error FROM jobs WHERE dedupe='low-1'").fetchone()
        self.assertEqual(row['status'], 'rejected')
        self.assertIn('8%', row['error'])

        calls.clear()
        printing._NOTICE_AT.pop('LowQ', None)
        blocked={'state':'Idle','reasons':['Out of paper'],'pages':None,'supplies':[{'name':'Black Toner','percent':40}],'serial':'','firmware':''}
        with patch.object(printing,'printer_reachable',return_value=True), patch.object(printing,'printer_stats',return_value=blocked), patch.object(printing,'cmd',side_effect=fake_cmd):
            with self.assertRaises(printing.PrintError) as paper:
                printing.submit_file('paper.pdf', b'%PDF-1.4', 'LowQ', dedupe='low-paper')
        self.assertIn('out of paper', str(paper.exception))
        self.assertEqual(calls, [])

        msg=EmailMessage()
        msg['From']='person@example.com'
        msg['Subject']='PRINT'
        msg.set_content('file')
        msg.add_attachment(b'%PDF-1.4', maintype='application', subtype='pdf', filename='note.pdf')
        cfg={'allowed_senders':'','mail_recipient':'','email_pin':'','default_sides':'one-sided','max_files':'5','default_queue':'LowQ','print_email_body':'0'}
        with patch.object(printing,'printer_reachable',return_value=True), patch.object(printing,'printer_stats',return_value=stats), patch.object(printing,'cmd',side_effect=fake_cmd):
            result=mailer.handle_message(msg, 'low-mail', cfg)
        self.assertEqual(result['jobs'], 0)
        self.assertIn('8%', result['reason'])
        html=mailer.render_reply(result)
        self.assertIn('NOT PRINTED', html)
        self.assertIn('8%', html)
        self.assertIn('not printed', html.lower())

    def test_result_email_includes_the_printer_photo(self):
        self._reset_images()
        png=self._png()
        ident=images.identify('Brother HL-L2350DW series')
        path=images._cache_file(ident['key'])
        path.write_bytes(png)
        images._save_catalog({
            'model_key':ident['key'],'manufacturer':'brother','model':ident['code'],'source':'printer',
            'fingerprint_id':'','local_path':str(path),'confidence':1,'license':'','attribution':'The printer',
            'source_page':'','checked_at':'','negative_until':'','category':'mono','candidates':'[]',
        })
        with store.connection() as db:
            db.execute("INSERT OR REPLACE INTO printers(queue,label,host,protocol,identity) VALUES('PhotoQ','Office Printer','192.0.2.71','socket','Brother HL-L2350DW series')")
        data, subtype=images.email_bytes('PhotoQ')
        self.assertEqual(subtype, 'png')
        self.assertTrue(data.startswith(b'\x89PNG'))
        msg=EmailMessage()
        msg['From']='Person <person@example.com>'
        msg['Subject']='PRINT'
        msg['Message-ID']='<photo@example.com>'
        config={
            'reply_enabled':'1','smtp_from':'printme@example.com','imap_user':'devon@example.com',
            'imap_password':'secret-pass','smtp_host':'smtp.gmail.com','smtp_port':'587',
        }
        result={'accepted':False,'jobs':0,'errors':0,'reason':'Black Toner is at 8%. The document was not printed.','queue':'PhotoQ','notes':[],'files':[]}

        class FakeSMTP:
            def __init__(self, host, port, timeout=None):
                FakeSMTP.last=self
            def ehlo(self):
                return None
            def starttls(self):
                return None
            def login(self, user, password):
                return None
            def send_message(self, message):
                self.message=message
            def quit(self):
                return None

        with patch('app.mailer.smtplib.SMTP', FakeSMTP):
            mailer.notify_sender(msg, config, result)
        sent=FakeSMTP.last.message
        html=sent.get_body(preferencelist=('html',)).get_content()
        plain=sent.get_body(preferencelist=('plain',)).get_content()
        self.assertIn('cid:printer', html)
        self.assertIn('8%', html)
        self.assertIn('8%', plain)
        self.assertTrue(any(part.get_content_type()=='image/png' for part in sent.walk()))

    def test_device_icon_is_cached_for_the_model(self):
        self._reset_images()
        self._enable_images()
        png=self._png()
        with store.connection() as db:
            db.execute("INSERT OR REPLACE INTO printers(queue,label,host,protocol,identity) VALUES('IconQ','Office','192.0.2.72','socket','Brother HL-L2350DW series')")

        class Resp:
            headers={'Content-Type':'image/png'}
            def read(self, _limit):
                return png
            def geturl(self):
                return 'http://192.0.2.72/icon.png'
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False

        try:
            with patch.object(printing,'ipp_attributes',return_value={'printer-icons':['http://192.0.2.72/icon.png']}), patch('app.images.urllib.request.build_opener') as build:
                build.return_value.open.return_value=Resp()
                row=images.resolve_printer('IconQ')
            self.assertEqual(row['source'], 'printer')
            self.assertTrue(Path(row['local_path']).is_file())
            self.assertEqual(images.file_for('IconQ')[1], 'image/png')
        finally:
            self._disable_images()

if __name__=='__main__': unittest.main()
