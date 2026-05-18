import logging
from celery import current_app
from app.tasks.base_image import run_image_pipeline, AssetPipelineConfig

logger = logging.getLogger("doers")

@current_app.task(bind=True, max_retries=3)
def process_org_cover(self, org_id: str, upload_id: str, user_id: str, request_ip: str = None, focal_y: float = 0.5):
    config = AssetPipelineConfig(
        asset_type="cover",
        sizes={"mobile": 640, "tablet": 1024, "desktop": 1920},
        resize_strategy="focal_crop",
        allow_svg=False,  # Hard-banned for covers
        max_size_bytes=10_485_760,  # 10MB
        min_width=1200,
        min_height=400,
        focal_y=focal_y
    )
    run_image_pipeline(
        org_id=org_id,
        upload_id=upload_id,
        user_id=user_id,
        request_ip=request_ip,
        config=config
    )
