# coding: utf-8
__author__ = 'seanlook.com'

import MySQLdb
import os, sys, time, datetime
import commands
import ConfigParser
import threading
from threading import Thread, local
import re
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
import json
from collections import defaultdict
import settings
import prpcryptec

from logging.handlers import TimedRotatingFileHandler


LOG_FILE = 'killquery.log'
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

#handler = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=1024*1024, backupCount=5)
handler = TimedRotatingFileHandler(LOG_FILE, when='d', interval=1, backupCount=7)

formatter = logging.Formatter('%(asctime)s [%(levelname)-7s] %(threadName)6s >> %(message)s')

handler.setFormatter(formatter)

logger.addHandler(handler)

THREAD_DATA = local()
#KEY_DB_AUTH = "your_16_bytes_key"

# get configuration section
# db_commkill: common config and can be overwritten (inherit)
# db_commconfig: common info and not inherit
def get_setttings(sect, opt=''):
    cf = ConfigParser.ConfigParser()
    cf.read(settings.CONFIG_FILE_PATH)

    if opt != '':
        o = cf.get(sect, opt)
        return o
    # 获得具体 db实例的kill信息，section必须以 id_开头
    if re.match('id_', sect):
        v1 = dict(cf.items("db_commkill"))
        try:
            v2 = dict(cf.items(sect))
        except ConfigParser.NoSectionError:
            logger.debug("no section %s found in %s, use comm section", sect, settings.CONFIG_FILE_PATH)
            v2 = v1

        v2 = dict(v1, **v2)

        # 将要执行kill的user转成 list
        if 'k_user' in v2:
            k_users = v2['k_user'].replace(' ', '').split(',')
            v2['k_user'] = k_users

        # 匹配和排除规则，转化成python regex object
        if 'k_exclude' in v2:
            k_exclude = re.compile(v2['k_exclude'])
            v2['k_exclude'] = k_exclude
        if 'k_include' in v2:
            # print v2['k_include']
            k_include = re.compile(v2['k_include'])
            v2['k_include'] = k_include
    else:
        v2 = dict(cf.items(sect))

        # 运行的时间窗口，取得开始和结束时间
        if 'run_time_window' in v2:
            run_time_window = v2['run_time_window'].replace(' ', '').split('-')
            if len(run_time_window) != 2:
                v2['run_time_window'] = []
            else:
                v2['run_time_window'] = run_time_window

    return v2

# get processlist to check connection session
#
def get_processlist_kthreads(conn, kill_opt, db_id):
    processlist_file = 'var/processlist_' + db_id + '.txt'
    logger.debug("get the information_schema.processlist on this moment: %s", processlist_file)

    threads_tokill = defaultdict(list)
    try:
        cur = conn.cursor()
        sqlstr = "select * from information_schema.processlist order by time desc"

        cur.execute(sqlstr)
        rs = cur.fetchall()

    except MySQLdb.Error, e:
        logger.critical("Get processlist connection error. Wait ping alive to reconnect.")
    else:
        fo = open(processlist_file, "w")
        fo.write("\n\n################  " + time.asctime() + "  ################\n")

        logger.debug("check this conn thread according to kill_opt one by one")

        for row in rs:

            iskill_thread = kill_judge(row, kill_opt)
            if iskill_thread > 0:
             #   if row[1] not in threads_tokill:
             #       threads_tokill[row[1]] = []
                threads_tokill[row[1]].append(iskill_thread)

                fo.write(str(row) + "\n")
            # print str(row)

        fo.close()
    finally:
        cur.close()

    return threads_tokill

def db_reconnect(db_user, db_id):
    db_pass = settings.DB_AUTH[db_user]
    pc = prpcryptec.prpcrypt(KEY_DB_AUTH)

    db_instance = get_setttings("db_info", db_id)
    db_host, db_port = db_instance.replace(' ', '').split(':')

    db_conn = None

    while not db_conn:
        try:
            logger.warn("Reconnect Database %s: host='%s', user='%s, port=%s",
                        db_id, db_host, db_user, db_port)
            db_conn = MySQLdb.Connect(host=db_host, user=db_user, passwd=pc.decrypt(db_pass), port=int(db_port),
                                      connect_timeout=5, use_unicode=False)

        except MySQLdb.Error, e:

            if e.args[0] in (2013, 2003):
                logger.critical('Error %d: %s', e.args[0], e.args[1])
                logger.warn("Reconnect Database %s: host='%s', user='%s, port=%s",
                            db_id, db_host, db_user, db_port)
                db_conn = MySQLdb.Connect(host=db_host, user=db_user, passwd=pc.decrypt(db_pass), port=int(db_port),
                                          connect_timeout=5, use_unicode=False)

        except Exception as inst:
            print "Error %s %s" % type(inst), inst.args.__str__()

        time.sleep(10)

    return db_conn


# judge this thread meet kill_opt or not
def kill_judge(row, kill_opt):
    if (row[1] in kill_opt['k_user'] or 'all' in kill_opt['k_user']) \
            and not kill_opt['k_exclude'].search(str(row)):

        if kill_opt['k_include'].search(str(row)):
            return int(row[0])

        if int(kill_opt['k_sleep']) == 0 and row[4] == 'Sleep':
            return 0
        elif 0 < int(kill_opt['k_sleep']) < row[5] and row[4] == 'Sleep':
            return int(row[0])
        elif row[4] != 'Sleep':
            if 0 < int(kill_opt['k_longtime']) < row[5]:
                return int(row[0])

        return 0
    else:
        return 0


# take snapshot to gather more info before kill
def get_more_info(conn, threadName):
    logger.info("Gather info before kill using the same connection START")

    str_fulllist = "show full processlist"
    str_status = "show engine innodb status"
    str_trx_lockwait = """
        SELECT
        	r.trx_id waiting_trx_id,
        	r.trx_mysql_thread_id waiting_thread,
        	r.trx_query waiting_query,
        	r.trx_wait_started,
        	r.trx_operation_state,
        	b.trx_id blocking_trx_id,
        	b.trx_mysql_thread_id blocking_thread,
        	b.trx_query blocking_query,
        	b.trx_started,
        	b.trx_rows_locked,
        	b.trx_tables_locked,
        	b.trx_isolation_level
        FROM
        	information_schema.innodb_lock_waits w
        INNER JOIN information_schema.innodb_trx b ON b.trx_id = w.blocking_trx_id
        INNER JOIN information_schema.innodb_trx r ON r.trx_id = w.requesting_trx_id
    """
    try:
        cur = conn.cursor()

        snapshot_file = "var/snapshot_" + threadName + ".txt"
        fo = open(snapshot_file, "a")
        fo.write("\n\n######################################################\n")
        fo.write("##############  " + time.asctime() + "  ##############\n")
        fo.write("######################################################\n")


        logger.debug("Get 'show full processlist' to: %s", snapshot_file)
        fo.write("\n######## show full processlist : ########\n")
        cur.execute(str_fulllist)
        rs = cur.fetchall()
        for row in rs:
            fo.write(str(row))
            fo.write("\n")

        logger.debug("Get 'innodb_lock_waits' to: %s", snapshot_file)
        fo.write("\n\n######## innodb_lock_waits : ########\n")
        fo.write("waiting_trx_id waiting_thread waiting_query wait_started operation_state"
                 "  blocking_trx_id blocking_thread blocking_query trx_started rows_locked tables_locked isolation_level\n")
        cur.execute(str_trx_lockwait)
        rs = cur.fetchall()
        for row in rs:
            fo.write(str(row))
            fo.write("\n")


        logger.debug("Get 'show engine innodb status' to: %s", snapshot_file)
        fo.write("\n\n######## show engine innodb status : ########\n")
        cur.execute(str_status)
        rs = cur.fetchone()

        fo.write(rs[2])

        fo.close()
    except MySQLdb.Error, e:
        logger.critical('Error %d: %s', e.args[0], e.args[1])
    finally:
        cur.close()
        fo.close()

def output_db():
    pass


def kill_threads(threads_tokill, db_conns, db_id, db_commconfig):
    # 没有需要被 kill 的会话
    if len(threads_tokill) == 0:
        logger.debug("no threads need to be kill")
        return 0

    logger.warn("this threads COULD be killed: %s", threads_tokill.__str__())

    process_user = db_commconfig['db_puser']

    # 记录需要被 kill 的 thread_id,主要用于判断是否重复发邮件
    for u, t_id in threads_tokill.items():
        kill_str = ";  ".join("kill %d" % t for t in t_id)
        thread_ids = set(t_id)

        # 明确设置dry_run=0才真正kill
        if db_commconfig['dry_run'] == '0':
            try:
                get_more_info(db_conns[process_user], db_id)
                sendemail(db_id, ' (' + u + ') KILLED')

                logger.info("(%s) run in dry_run=0 mode , do really kill, but the status snapshot is taken", u)
                cur = db_conns[u].cursor()
                cur.execute(kill_str)
                logger.warn("(%s) kill-command has been executed : %s", u, kill_str)
            except MySQLdb.Error, e:
                logger.critical('Error %d: %s', e.args[0], e.args[1])
            finally:
                cur.close()

        else:
            # dry_run模式下可能会反复或者同样需被kill的thread
            logger.info("(%s) run in dry_run=1 mode , do not kill, but take status snapshot the first time", u)

            # 前后两次 threads_tokill里面有共同的id，则不发送邮件
            if thread_ids and not (THREAD_DATA.THREADS_TOKILL.get(u,set()) & thread_ids):
                get_more_info(db_conns[process_user], db_id)
                sendemail(db_id, ' (' + u + ') NOT KILLED')

        # store last threads(kill or not kill)
        THREAD_DATA.THREADS_TOKILL[u] = thread_ids

# 邮件通知模块
def sendemail(db_id, dry_run):
    MAIL_CONFIG = get_setttings('mail_config')
    mail_receiver = MAIL_CONFIG['mail_receiver']
    mailenv = MAIL_CONFIG['env']

    if mail_receiver == "":
        logger.info("do not send email")
        return

    mail_host = MAIL_CONFIG['mail_host']
    mail_user = MAIL_CONFIG['mail_user']
    mail_pass = MAIL_CONFIG['mail_pass']

    message = MIMEMultipart()

    message['From'] = Header('mysql', 'utf-8')
    message['To'] = Header('DBA', 'utf-8')
    subject = '(%s) %s slow query has been take snapshot' % (mailenv, db_id)
    message['Subject'] = Header(subject, 'utf-8')

    message.attach(MIMEText('db有慢查询, threads <strong>' + dry_run + '</strong> <br/>', 'html', 'utf-8'))
    message.attach(MIMEText('<br/>You can find more info(snapshot) in file: <strong> var/snapshot_' +
                            db_id + '.txt </strong> processlist:<br/><br/>', 'html', 'utf-8'))

    with open("var/processlist_"+db_id+'.txt', 'rb')as f:
        filecontent = f.readlines()
    att1 = MIMEText("<br/>".join(filecontent), 'html', 'utf-8')
    #att1["Content-Type"] = 'application/octet-stream'
    #att1["Content-Disposition"] = 'attachment; filename="var/processlist_' + db_id + '.txt"'
    message.attach(att1)

    try:
        smtpObj = smtplib.SMTP(mail_host, port=25, timeout=3)
        # smtpObj.connect(mail_host, 25)
        smtpObj.ehlo()
        smtpObj.login(mail_user, mail_pass)
        smtpObj.sendmail(mail_user, mail_receiver, message.as_string())

        logger.info("Email sending succeed")
    except smtplib.SMTPException, err:
        logger.critical("Error email content: %s", message.as_string())
        logger.critical("Error: 发送邮件失败(%s, %s)", err[0], err[1].__str__())
    finally:
        smtpObj.quit()


# for db_instance one python thread: main function
def my_slowquery_kill(db_instance):
    db_id = db_instance[0]
    db_host, db_port = db_instance[1].replace(' ', '').split(':')
    #print "db_id, db_host, db_port" + db_id+db_host+db_port

    db_commconfig = get_setttings("db_commconfig")

    # 获取具体的db_instance 选项kill
    kill_opt = get_setttings("id_" + db_id)

    # 登录db认证信息
    #db_users = json.loads(db_commconfig["db_auth"])
    db_users = settings.DB_AUTH

    # 每个db实例的多个用户维持各自的连接
    db_conns = {}

    # db连接密码解密
    pc = prpcryptec.prpcrypt(KEY_DB_AUTH)
    for db_user, db_pass in db_users.items():
        dbpass_de = pc.decrypt(db_pass)
        try:
            conn = MySQLdb.Connect(host=db_host, user=db_user, passwd=dbpass_de, port=int(db_port), connect_timeout=5, use_unicode=False)
            db_conns[db_user] = conn
            logger.info("connection is created: %s:%s  %s", db_host, db_port, db_user)

        except MySQLdb.Error, e:
            logger.warn('Error %d: %s', e.args[0], e.args[1])
            sys.exit(-1)

    kill_count = 0
    run_max_count_last = 0
    check_ping_wait = 0

    while True:
        db_commconfig = get_setttings("db_commconfig")

        # 查看processlist连接的作为心跳
        # 如果数据库端 kill掉这个用户的连接，该实例检查则异常退出
        if (db_commconfig['run_time_window'][0] < datetime.datetime.now().strftime("%H:%M") < db_commconfig['run_time_window'][1])\
                or len(db_commconfig['run_time_window']) == 0:
            run_max_count = int(db_commconfig['run_max_count'])
            if run_max_count != run_max_count_last:
                logger.info("you've changed run_max_count, set a clean start")
                kill_count = 0
                THREAD_DATA.THREADS_TOKILL = {}

                if run_max_count == 999:
                    logger.warn("you've set run_max_count=999 , always check processlist")
                    # kill_count = 0
                if run_max_count == 0:
                    logger.info("you've set run_max_count=0 , stop check processlist & keep user conn alive")
                    run_max_count_last = run_max_count

            if run_max_count == 999:
                kill_count = 0

            if kill_count < run_max_count:
                threads_tokill = get_processlist_kthreads(db_conns[db_commconfig['db_puser']], kill_opt, db_id)

                kill_threads(threads_tokill, db_conns, db_id, db_commconfig)

                kill_count += 1
                run_max_count_last = run_max_count
        else:
            logger.debug("Not running in time window")
            # fix: 处理慢sql在夜间产生，并持续到白天的情况
            THREAD_DATA.THREADS_TOKILL = {}
            kill_count = 0

        time.sleep(settings.CHECK_CONFIG_INTERVAL)
        # 维持其它用户连接的心跳，即使被kill也会被拉起
        if check_ping_wait == settings.CHECK_PING_MULTI:
            for dc_user in db_conns:
                try:
                    logger.info("(%s) MySQL ping to keep session alive", dc_user)
                    db_conns[dc_user].ping()
                except MySQLdb.Error, e:
                    logger.critical('Error %d: %s', e.args[0], e.args[1])

                    db_conns[dc_user] = db_reconnect(dc_user, db_id)

            check_ping_wait = 0
        else:
            check_ping_wait += 1

        kill_opt = get_setttings("id_" + db_id)


# use multi-thread
class myThread(threading.Thread):
    def __init__(self, threadID, db_instance):
        threading.Thread.__init__(self)
        self.threadID = threadID
        self.name = db_instance[0]

    def run(self):
        logger.info("Starting kill query Thread: %s", self.name)
        #THREAD_DATA.MAIL_SEND_TIMES = 0
        THREAD_DATA.THREADS_TOKILL = {}

        my_slowquery_kill(db_instance)
        logger.info("Exiting Thread: %s", self.name)

if __name__ == '__main__':
    db_instances = get_setttings("db_info")
    # like {"crm1": "10.0.200.196:3306", "crm2": "10.0.200.199:3306"}

    # start keep-session-kill threads for every user and db_instance
    for db_instance in db_instances.items():
        # threadName like dbnqqame_user
        thread_to_killquery = myThread(100, db_instance)
        thread_to_killquery.start()
        time.sleep(0.8)