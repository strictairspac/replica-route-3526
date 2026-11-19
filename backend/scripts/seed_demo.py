from sqlalchemy import select,func
from src.config.database import Session,Base,engine
from src.models.entities import User,Node,RequestSet,Trial,CommitTrial
from src.utils.security import password_hash

def seed():
    Base.metadata.create_all(engine)
    with Session() as db:
        for role in ('admin','operator','viewer'):
            email=role+'@qiyuan.test'
            if not db.scalar(select(User).where(User.email==email)):db.add(User(email=email,password=password_hash('Demo2026!'),role=role))
        for name,role,pos,delay,note in [('harbor-primary','primary',105,0,'栖原主站 · 写入航道'),('north-replica','replica',104,150,'北站副本 · 延迟偏高'),('south-replica','replica',105,20,'南站副本 · 常用读航道')]:
            if not db.scalar(select(Node).where(Node.name==name)):db.add(Node(name=name,role=role,status='healthy',lineage='qiyuan-log-01',position=pos,delay_ms=delay,note=note))
        mixed=[{'at_ms':0,'kind':'write','session':'lab-A'},{'at_ms':100,'kind':'read','session':'lab-A'},{'at_ms':600,'kind':'replicate','node':'south-replica','position':106,'delay_ms':20},{'at_ms':700,'kind':'read','session':'lab-A'},{'at_ms':800,'kind':'node_down','node':'south-replica'},{'at_ms':900,'kind':'read','session':'lab-A'},{'at_ms':1000,'kind':'node_down','node':'harbor-primary'},{'at_ms':1100,'kind':'read','session':'lab-A'}]
        split=[{'at_ms':0,'kind':'commit','session':'lab-A','order':'SO-8801'},
               {'at_ms':20,'kind':'replicate','node':'south-replica','table':'orders','batch':'po-106','position':106},
               {'at_ms':30,'kind':'query','session':'lab-A','order':'SO-8801'},
               {'at_ms':40,'kind':'commit','session':'lab-A','order':'SO-8802'},
               {'at_ms':50,'kind':'replicate','node':'south-replica','table':'orders','batch':'po-107','position':107},
               {'at_ms':60,'kind':'query','session':'lab-A','order':'SO-8802'},
               {'at_ms':70,'kind':'replicate','node':'south-replica','table':'order_lines','batch':'pl-107','position':107},
               {'at_ms':80,'kind':'replicate','node':'south-replica','table':'order_lines','batch':'pl-106','position':106},
               {'at_ms':90,'kind':'replicate','node':'south-replica','table':'order_lines','batch':'pl-107','position':107},
               {'at_ms':100,'kind':'commit','session':'lab-A','order':'SO-8802'},
               {'at_ms':105,'kind':'replicate','node':'south-replica','table':'orders','batch':'po-108','position':108},
               {'at_ms':110,'kind':'node_down','node':'harbor-primary'},
               {'at_ms':120,'kind':'query','session':'visitor','order':'SO-8802'},
               {'at_ms':130,'kind':'query','session':'lab-A','order':'SO-8802'},
               {'at_ms':900,'kind':'replicate','node':'south-replica','table':'order_lines','batch':'pl-108','position':108},
               {'at_ms':950,'kind':'query','session':'lab-A','order':'SO-8802'}]
        cases=[('QY-1117-A','交接班写后读与双节点故障',mixed),('QY-1117-B','延迟超标回退主库',[{'at_ms':0,'kind':'node_down','node':'south-replica'},{'at_ms':10,'kind':'read','session':'visitor'}]),('QY-1117-C','双副本不可读时拒绝',[{'at_ms':0,'kind':'write','session':'lab-B'},{'at_ms':1000,'kind':'node_down','node':'harbor-primary'},{'at_ms':1100,'kind':'read','session':'lab-B'}]),('QY-1118-D','订单头与商品行分开追赶',split)]
        for code,title,events in cases:
            if not db.scalar(select(RequestSet).where(RequestSet.code==code)):db.add(RequestSet(code=code,title=title,events=events,note='栖原站点值班交接 · 09:15 请求轨迹' if code!='QY-1118-D' else '订单与商品行分行复制 · 提交视点复核'))
        db.commit();print({cls.__tablename__:db.scalar(select(func.count()).select_from(cls)) for cls in (User,Node,RequestSet,Trial,CommitTrial)})
if __name__=='__main__':seed()
