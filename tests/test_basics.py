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

from app import mailer, printing, store

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
        with patch.object(printing,'cmd',return_value=SimpleNamespace(returncode=0,stdout='request id is Dads_Brother-34 (1 file(s))',stderr='')) as fake:
            out=mailer.handle_message(msg,'test-mail-uid-1',cfg)
            self.assertEqual(out['jobs'],1)
            self.assertIn('sides=two-sided-long-edge',fake.call_args.args)
            self.assertIn('2',fake.call_args.args)
            out2=mailer.handle_message(msg,'test-mail-uid-1',cfg)
            self.assertEqual(out2['jobs'],1)
            self.assertEqual(fake.call_count,1)  # duplicate never re-submitted

    def test_queue_input_validation(self):
        with self.assertRaises(printing.PrintError):
            printing.add_printer('Bad','../evil','127.0.0.1','ipp')

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
            self.assertEqual(cfg['email_pin'],'abc12345')
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
        self.assertEqual(mailer.summarize_poll(0,0)[0],'Healthy')
        self.assertEqual(mailer.summarize_poll(2,1)[0],'Attention')

if __name__=='__main__': unittest.main()
