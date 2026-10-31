"""Unit and route smoke tests. Run with `python -m unittest discover -s tests -v`."""
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
_temp=tempfile.TemporaryDirectory(prefix='printharbor-test-')
os.environ['DATA_DIR']=_temp.name

from app import mailer, printing, store, supplies, tailnet

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
        self.assertEqual(brother['driver'],'Brother driver, darkest setting')
        epson=printing.choose_driver({
            'reachable':True,'ipp':False,'socket':True,'make':'Epson','model':'ET-2760','identity':'Epson ET-2760',
        }, drivers)
        self.assertEqual(epson['model'],'gutenprint.5.3://epson/epson-et-2760')
        self.assertEqual(epson['protocol'],'socket')
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
        self.assertIn('Toner, ink, and page count, read when this page opens.', page.text)

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
        self.assertIn('Status, toner, and page count are read when this page opens.', page.text)

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
        self.assertIn(('lpadmin','-p','FadeFix','-o','brlaserEconomode=False'),calls)
        self.assertIn(('lpadmin','-p','FadeFix','-o','brlaserDensityAdjust=106'),calls)
        joined=' '.join(' '.join(args) for args in calls)
        self.assertNotIn('-m everywhere', joined)
        with store.connection() as db:
            row=db.execute("SELECT protocol, identity, driver FROM printers WHERE queue='FadeFix'").fetchone()
            db.execute("DELETE FROM printers WHERE queue='FadeFix'")
        self.assertEqual(row['protocol'],'socket')
        self.assertEqual(row['identity'],'Brother HL-L2380DW series')
        self.assertIn('Brother driver', row['driver'])

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
            self.assertEqual(client.get('/login').status_code,200)
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
                ('/?page=email','Approved sender emails'),
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
        self.assertEqual(mailer.snapshot()['detail'], 'Turn on automatic email printing below, then save.')
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as client:
            client.post('/login',data={'username':'admin','password':os.environ['ADMIN_PASSWORD']},follow_redirects=False)
            page=client.get('/?page=email')
            self.assertIn('Turn on automatic email printing below, then save.', page.text)
            self.assertIn('Enable automatic email printing', page.text)
            self.assertIn('Leave blank to accept mail from any sender', page.text)
            self.assertIn('Leave blank to accept any To or Cc', page.text)
            self.assertIn('Leave blank to require no PIN', page.text)
            self.assertIn('Mail already in the folder is left alone', page.text)
            self.assertLess(page.text.find('Enable automatic email printing'), page.text.find('IMAP server'))

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

if __name__=='__main__': unittest.main()
