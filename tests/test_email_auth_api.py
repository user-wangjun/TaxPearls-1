"""Public G11 HTTP contract: transport mocked, real cookie/proof/store pipeline."""
import auth_support
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs,urlsplit

from fastapi.testclient import TestClient

from src import mailer
from webapp import app as module, email_auth, members, invitations
from webapp.captcha import issue as captcha_issue
from webapp.login_guard import RateLimiter
from webapp.storage import Store


class EmailAuthApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'api.db')
        self.password='Secure-browser-2026!'
        self.root=self.store.create_user('root',self.password,'平台','platform_admin','platform','chief@example.com')
        self.owner=self.store.create_user('owner',self.password,'机构','org_admin','alpha','owner@example.com')
        members.set_quota(self.store,self.root,'alpha',5,0)
        self.mail=[]
        def capture(**kwargs):self.mail.append(kwargs);return 'mock-mail'
        patched=patch.multiple(module,store=self.store,send_registration_code_email=capture,
            send_login_code_email=capture,send_password_reset_email=capture,
            register_code_limiter=RateLimiter({'email':(50,900),'ip':(500,3600)}),
            reset_limiter=RateLimiter({'email':(50,900),'ip':(500,3600)}),
            register_complete_limiter=RateLimiter({'email':(10,900),'ip':(60,3600)}),
            reset_confirm_limiter=RateLimiter({'token':(5,900),'ip':(40,900)}),
            email_verify_limiter=RateLimiter({'token':(10,900),'ip':(60,900)}),
            email_login_limiter=RateLimiter({'email':(10,900),'ip':(60,900)}))
        patched.start();self.addCleanup(patched.stop)
        env=patch.dict(os.environ,{'TAXPEARLS_PUBLIC_BASE_URL':'','TAXPEARLS_COOKIE_SECURE':'0',
            'TAXPEARLS_AI_ENABLED':'0','TAXPEARLS_NOTIFICATION_EMAIL_ENABLED':'0'})
        env.start();self.addCleanup(env.stop)
        self.client=TestClient(module.app,base_url='http://localhost')
        self.client.__enter__();self.addCleanup(self.client.__exit__,None,None,None)

    def start(self,email,purpose='register',invite='',client=None):
        challenge=captcha_issue('AB2D')
        return (client or self.client).post('/api/auth/email/start',json={'email':email,'purpose':purpose,
            'invite_code':invite,'captcha_id':challenge['captcha_id'],'captcha_answer':'ab2d'})

    def token(self):
        url=self.mail[-1].get('signup_url') or self.mail[-1]['reset_url']
        self.assertEqual(urlsplit(url).query,'')
        return parse_qs(urlsplit(url).fragment)['email'][0]

    def verify(self,token=None,client=None):
        return (client or self.client).post('/api/auth/email/verify',json={'token':token or self.token()})

    def register(self,email,code='',invite='',proof='',client=None):
        return (client or self.client).post('/api/register/complete',json={'email':email,'code':code,
            'invite_code':invite,'email_proof':proof,'password':self.password})

    def test_magic_registration_cookie_binding_no_get_consumption_and_saved_invite(self):
        link=invitations.issue(self.store,self.owner,'alpha','student')
        started=self.start('student@example.com',invite=link['code'])
        self.assertEqual(started.status_code,200,started.text)
        cookie=started.headers['set-cookie']
        self.assertIn('HttpOnly',cookie);self.assertIn('SameSite=strict',cookie)
        self.assertNotIn(self.mail[-1]['code'],started.text);self.assertNotIn(self.token(),started.text)
        token=self.token()
        self.assertEqual(len(token),43)
        self.assertEqual(self.client.get('/#email='+token).status_code,200)
        self.assertEqual(self.client.get('/api/auth/email/verify',params={'token':token}).status_code,405)
        with TestClient(module.app,base_url='http://localhost') as stranger:
            self.assertEqual(self.verify(token,stranger).status_code,422)
        verified=self.verify(token);self.assertEqual(verified.status_code,200,verified.text)
        self.assertEqual(verified.json()['email'],'student@example.com');self.assertTrue(verified.json()['has_invite'])
        self.assertEqual(self.verify(token).status_code,422)
        done=self.register('student@example.com',proof=verified.json()['proof'])
        self.assertEqual(done.status_code,200,done.text)
        self.assertEqual(done.json()['user']['role'],'student');self.assertEqual(done.json()['org_id'],'alpha')
        self.assertEqual(self.client.get('/api/me').status_code,200)

    def test_code_registration_rejects_other_browser_and_legacy_unbound_codes(self):
        link=invitations.issue(self.store,self.owner,'alpha','accountant')
        self.start('member@example.com',invite=link['code']);code=self.mail[-1]['code']
        with TestClient(module.app,base_url='http://localhost') as stranger:
            self.assertEqual(self.register('member@example.com',code,link['code'],client=stranger).status_code,422)
        self.assertEqual(self.register('member@example.com',code,link['code']).status_code,200)
        legacy=auth_support.legacy_token(self.store,'legacy@example.com','register')
        self.assertEqual(self.register('legacy@example.com',legacy,link['code']).status_code,422)
        self.assertIsNone(self.store.get_user_by_email('legacy@example.com'))

    def test_email_login_code_and_magic_use_real_http_only_sessions_once(self):
        self.assertEqual(self.start('owner@example.com','login').status_code,200)
        code=self.mail[-1]['code'];token=self.token()
        login=self.client.post('/api/auth/email/login',json={'email':'owner@example.com','code':code})
        self.assertEqual(login.status_code,200,login.text)
        session=login.cookies.get(module.COOKIE_NAME)
        self.assertNotIn(session,login.text);self.assertIn('HttpOnly',login.headers['set-cookie'])
        self.assertEqual(self.client.get('/api/me').json()['id'],self.owner['id'])
        self.assertEqual(self.verify(token).status_code,422)
        self.start('chief@example.com','login')
        magic=self.verify();self.assertEqual(magic.status_code,200,magic.text)
        self.assertNotIn('session',magic.json());self.assertEqual(magic.json()['user']['role'],'platform_admin')
        self.assertEqual(self.client.get('/api/me').json()['id'],self.root['id'])

    def test_unknown_disabled_login_and_transport_failure_have_same_public_response(self):
        known=self.start('owner@example.com','login')
        unknown=self.start('unknown@example.com','login')
        self.assertEqual(known.json(),unknown.json());self.assertEqual(len(self.mail),1)
        with self.store.connect() as db:db.execute('UPDATE users SET active=0 WHERE id=?',(self.owner['id'],))
        disabled=self.start('owner@example.com','login');self.assertEqual(known.json(),disabled.json())
        with patch.object(module,'send_login_code_email',side_effect=mailer.MailError('upstream secret RAW_TOKEN')):
            failed=self.start('chief@example.com','login')
        self.assertEqual(known.json(),failed.json());self.assertEqual(failed.status_code,200)
        with self.store.connect() as db:
            self.assertNotIn('RAW_TOKEN',str([tuple(r) for r in db.execute('SELECT * FROM audit_log')]))
            self.assertIsNotNone(db.execute("SELECT used_at FROM email_tokens WHERE email='chief@example.com'").fetchone()[0])

    def test_reset_full_bound_flow_invalidates_old_session_and_legacy_reset_rejected(self):
        _,old=self.store.authenticate(self.owner['username'],self.password)
        response=self.client.post('/api/auth/password/reset',json={'email':'owner@example.com'})
        self.assertEqual(response.status_code,200,response.text)
        token=self.token()
        with TestClient(module.app,base_url='http://localhost') as stranger:
            self.assertEqual(self.verify(token,stranger).status_code,422)
        proof=self.verify(token).json()['proof']
        weak=self.client.post('/api/auth/password/reset/confirm',json={'token':proof,'password':'short'})
        self.assertEqual(weak.status_code,422)
        done=self.client.post('/api/auth/password/reset/confirm',json={'token':proof,'password':'Fresh-identity-2027!'})
        self.assertEqual(done.status_code,200,done.text);self.assertIsNone(self.store.user_for_token(old))
        self.assertIsNotNone(self.store.authenticate(self.owner['username'],'Fresh-identity-2027!'))
        legacy=auth_support.legacy_token(self.store,'owner@example.com','reset')
        self.assertEqual(self.client.post('/api/auth/password/reset/confirm',json={'token':legacy,'password':self.password}).status_code,422)

    def test_reset_failure_is_generic_and_logs_no_provider_payload(self):
        unknown=self.client.post('/api/auth/password/reset',json={'email':'unknown@example.com'})
        with patch.object(module,'send_password_reset_email',side_effect=mailer.MailError('SECRET_EMAIL_LINK')):
            failed=self.client.post('/api/auth/password/reset',json={'email':'owner@example.com'})
        self.assertEqual(failed.status_code,200);self.assertEqual(failed.json(),unknown.json())
        with self.store.connect() as db:
            self.assertNotIn('SECRET_EMAIL_LINK',str([tuple(r) for r in db.execute('SELECT * FROM audit_log')]))
            self.assertIsNotNone(db.execute("SELECT used_at FROM email_tokens WHERE purpose='reset'").fetchone()[0])

    def test_registration_delivery_failure_invalidates_credential_without_echoing_error(self):
        with patch.object(module,'send_registration_code_email',side_effect=mailer.MailError('SECRET_EMAIL_LINK')):
            result=self.start('failed@example.com')
        self.assertEqual(result.status_code,502);self.assertNotIn('SECRET_EMAIL_LINK',result.text)
        with self.store.connect() as db:
            self.assertIsNotNone(db.execute('SELECT used_at FROM email_tokens').fetchone()[0])
            self.assertNotIn('SECRET_EMAIL_LINK',str([tuple(r) for r in db.execute('SELECT * FROM audit_log')]))

    def test_host_injection_rejected_before_issuing_any_credentials(self):
        with TestClient(module.app,base_url='https://attacker.example') as attacker:
            response=attacker.post('/api/auth/password/reset',json={'email':'owner@example.com'})
            self.assertEqual(response.status_code,503)
            self.assertEqual(self.start('new@example.com',client=attacker).status_code,503)
        self.assertEqual(self.mail,[])
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM email_tokens').fetchone()[0],0)
        for value in ['http://public.example','https://user:secret@example.com','https://example.com/path',
                      'https://example.com/?token=LEAK','https://example.com/#LEAK','https://example.com:invalid']:
            with patch.dict(os.environ,{'TAXPEARLS_PUBLIC_BASE_URL':value}):
                result=self.client.post('/api/auth/password/reset',json={'email':'owner@example.com'})
                self.assertEqual(result.status_code,503);self.assertNotIn('LEAK',result.text)

    def test_configured_origin_and_secure_cookie_ignore_untrusted_host(self):
        with patch.dict(os.environ,{'TAXPEARLS_PUBLIC_BASE_URL':'https://tax.example','TAXPEARLS_COOKIE_SECURE':'1'}):
            with TestClient(module.app,base_url='https://untrusted.example') as client:
                response=self.start('secure@example.com',client=client)
                self.assertEqual(response.status_code,200,response.text)
                self.assertTrue(self.mail[-1]['signup_url'].startswith('https://tax.example/#email='))
                self.assertIn('Secure',response.headers['set-cookie'])
                self.assertEqual(self.verify(client=client).status_code,200)

    def test_verification_and_login_limits_precede_store_and_no_secret_echo(self):
        for route,attribute,method,payload in [('/api/auth/email/verify','email_verify_limiter','redeem_magic',{'token':'private-token'}),
            ('/api/auth/email/login','email_login_limiter','login_with_code',{'email':'owner@example.com','code':'123456'})]:
            dims={'token':(1,60),'ip':(2,60)} if 'verify' in route else {'email':(1,60),'ip':(2,60)}
            with patch.object(module,attribute,RateLimiter(dims)),patch.object(email_auth,method,side_effect=ValueError('invalid')) as operation:
                self.assertEqual(self.client.post(route,json=payload).status_code,422)
                rejected=self.client.post(route,json=payload)
                self.assertEqual(rejected.status_code,429);operation.assert_called_once()
                self.assertNotIn('private-token',rejected.text);self.assertNotIn('123456',rejected.text)
        with patch.object(email_auth,'redeem_magic') as operation:
            result=self.client.post('/api/auth/email/verify',json={'token':'Z'*200})
            self.assertEqual(result.status_code,422);self.assertNotIn('Z'*200,result.text);operation.assert_not_called()

    def test_logout_removes_initiating_cookie_and_new_browser_must_restart_flow(self):
        self.start('owner@example.com','login');token=self.token()
        self.client.post('/api/logout')
        self.assertIsNone(self.client.cookies.get(module.EMAIL_COOKIE_NAME))
        self.assertEqual(self.verify(token).status_code,422)

    def test_templates_use_independent_link_escape_html_and_explain_browser_binding(self):
        url='https://safe.example/#email=independent-token&extra="quoted"'
        with patch.object(mailer,'send_email',return_value='mail') as send:
            mailer.send_registration_code_email(to='a@example.com',code='123456',signup_url=url)
            html=send.call_args.kwargs['html']
            self.assertIn('同一浏览器',html);self.assertIn('123456',html);self.assertIn('&amp;extra=&quot;quoted&quot;',html)
            self.assertNotIn('123456',send.call_args.kwargs['subject'])
            mailer.send_login_code_email(to='a@example.com',code='654321',signup_url=url)
            self.assertIn('邮箱登录',send.call_args.kwargs['subject'])
            mailer.send_password_reset_email(to='a@example.com',reset_url=url)
            self.assertIn('同一浏览器',send.call_args.kwargs['html'])
