"""Ten fixed overnight rooms. They are not city places and never enter the scene pool.

At 18:00 an away-from-home character rolls one room for the whole night; the room
text is reused, while city/district/weather always come from the actual location.
"""
from copy import deepcopy
import random

OVERNIGHT_SCENES = (
    {"id": "chain_hotel", "name": "连锁酒店标准间", "sleep": "bed",
     "desc": "连锁酒店的标准间：浅米色墙纸配深灰地毯，两张并排的单人床铺着白色棉被和四个枕头，"
             "床头是软包靠背与两盏暖色阅读壁灯；窗边半拉着遮光帘和纱帘，靠墙电视柜上放着热水壶和纸杯，"
             "门后有行李架与门链，空调送风很轻，走廊灯从门缝下透进来，房间干净安静。"},
    {"id": "old_inn", "name": "老城区小旅馆单间", "sleep": "bed",
     "desc": "老城区小旅馆的单间：窄长房间，木门上挂着老式门牌和反锁链，单人铁架床铺着洗得发白的碎花床单，"
             "旧床头柜上一盏暖黄布罩台灯、搪瓷杯和电视遥控器；墙上挂钟滴答作响，窗式空调嗡嗡运转，"
             "窗下摆着旧木桌和塑料椅，地面是花砖，走廊偶尔传来脚步声。"},
    {"id": "hostel_four", "name": "青旅女生四人间", "sleep": "bunk",
     "desc": "青年旅舍的女生四人间：白墙房间里两张铁架上下铺，下铺靠墙围起床帘，床品是统一的深蓝格子，"
             "床头夹着小夜灯和充电插座；门边一排带锁储物柜，公共洗手台在走廊尽头，"
             "同房旅人已经睡下，房间只留一盏地灯。"},
    {"id": "hostel_six", "name": "青旅女生六人间", "sleep": "bunk",
     "desc": "青年旅舍的女生六人间：靠墙三张铁架上下铺配木梯，六个床位各有帘子和小编号，"
             "行李箱塞在床下，窗边长桌放着水杯和旅行指南；白墙贴着几张路线便签，公共卫浴在门外，"
             "深夜只剩走廊灯和偶尔翻身的轻响。"},
    {"id": "cyber_cafe", "name": "网吧包夜包厢", "sleep": "cyber",
     "desc": "网吧的包夜小包厢：一张电竞桌和两把黑色沙发椅，显示器还亮着游戏画面，机械键盘与耳机摆在桌边，"
             "旁边是泡面桶、矿泉水瓶和纸巾盒；墙面贴着深色吸音板，机箱灯和 RGB 灯带把房间照得发蓝，"
             "外套搭在椅背上，门帘半拉，通宵营业。"},
    {"id": "bathhouse", "name": "洗浴中心休息厅", "sleep": "lounge",
     "desc": "洗浴中心的休息厅：大通间里一排排深色皮质躺椅，每张配一条薄毯和一个小茶几，"
             "头顶灯光调得很暗，电视无声播放节目；四周有隔帘和绿植，储物柜手环放在茶几上，"
             "远处水声和脚步声隐约，空气温暖干燥。"},
    {"id": "attic_room", "name": "民宿阁楼房间", "sleep": "bed",
     "desc": "民宿顶层的小阁楼房间：斜屋顶露出木梁，靠窗一张双人床铺着米白棉麻床品，"
             "床头挂着藤编吊灯，旁边是小书架和落地衣架；木地板，天窗拉上遮光帘，"
             "墙角放着一只藤箱，窗台摆着干花。"},
    {"id": "studio_flat", "name": "短租公寓单间", "sleep": "bed",
     "desc": "短租公寓的单间：进门是小厨房和吧台，里侧一张双人床铺灰蓝色床品，"
             "白色墙面挂两幅装饰画，落地窗前有纱帘，旁边一张小沙发和折叠衣架；"
             "门口是电子门锁，楼下的城市灯光从窗帘缝里透进来。"},
    {"id": "capsule", "name": "胶囊旅馆睡眠舱", "sleep": "bed",
     "desc": "胶囊旅馆的睡眠舱：一格一格的白色睡眠舱，拉上遮光帘，舱内是软垫、枕头、薄被和一盏小阅读灯，"
             "舱壁有 USB 口和挂钩；舱外一排储物柜和换鞋凳，走廊灯带很暗，公共浴室在尽头。"},
    {"id": "station_guesthouse", "name": "车站附近招待所单间", "sleep": "bed",
     "desc": "车站附近老式招待所的单间：单人床靠墙，白床单上叠着军绿色毛毯，"
             "床头柜放着暖水瓶、搪瓷杯和一台小电视；窗对街道，浅色窗帘半拉，"
             "地面是水磨石，门后挂着衣帽钩，走廊顶灯昏黄。"},
)


def overnight_list():
    """Defensive copy for bootstrap/test UI; the canonical rooms stay immutable."""
    return [deepcopy(scene) for scene in OVERNIGHT_SCENES]


def pick_overnight(rng=None):
    """Roll one room for tonight. A real RandomSource is injectable for tests."""
    return deepcopy((rng or random).choice(OVERNIGHT_SCENES))


def scene_by_id(scene_id, rng=None):
    for scene in OVERNIGHT_SCENES:
        if scene["id"] == str(scene_id):
            return deepcopy(scene)
    return pick_overnight(rng)
