import csv
import json
import uuid
from datetime import datetime, timezone
from io import StringIO
from typing import List, Dict, Any, Optional, Tuple
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.core.exceptions import ValidationError, NotFoundError
from app.core.redis_client import redis_client  # assumes redis client available
from app.models.import_log import ImportLog, ImportStatus
from app.models.member import Member
from app.repositories.member_repo import MemberRepository
from app.services.member_service import MemberService
from app.schemas.member import MemberCreate
from app.utils.phone import normalize_phone
from app.core.logging import logger


class ImportService:
    """Service for bulk importing members from CSV files."""

    # Expected CSV columns (for template)
    TEMPLATE_COLUMNS = [
        "member_name",
        "phone",
        "email",
        "date_of_birth",
        "gender",
        "blood_group",
        "address",
        "notes"
    ]

    def __init__(self, session: AsyncSession):
        self.session = session
        self.member_repo = MemberRepository(session)
        self.member_service = MemberService(session)

    async def create_preview(
        self,
        gym_id: UUID,
        file_content: bytes,
        filename: str,
        created_by: UUID
    ) -> Dict[str, Any]:
        """
        Upload CSV file, parse, store preview data in Redis, create ImportLog record.
        
        Args:
            gym_id: Target gym UUID
            file_content: Raw bytes of uploaded CSV file
            filename: Original filename
            created_by: Staff UUID performing import
            
        Returns:
            Dictionary with import_id, columns_detected, row_count, sample_rows
        """
        # Decode CSV content
        try:
            csv_text = file_content.decode('utf-8-sig')
        except UnicodeDecodeError:
            raise ValidationError("File must be UTF-8 encoded", error_code="VALIDATION_ERROR")
        
        # Parse CSV
        try:
            csv_reader = csv.DictReader(StringIO(csv_text))
            rows = list(csv_reader)
        except Exception as e:
            raise ValidationError(f"Invalid CSV format: {str(e)}", error_code="VALIDATION_ERROR")
        
        if not rows:
            raise ValidationError("CSV file is empty", error_code="VALIDATION_ERROR")
        
        # Detect columns (headers)
        detected_columns = list(rows[0].keys()) if rows else []
        
        # Generate unique import ID
        import_id = str(uuid.uuid4())
        
        # Store preview data in Redis with 30min TTL
        preview_key = f"import_preview:{import_id}"
        preview_data = {
            "gym_id": str(gym_id),
            "filename": filename,
            "rows": rows,  # full list of dict rows
            "created_by": str(created_by),
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        await redis_client.setex(
            preview_key,
            1800,  # 30 minutes
            json.dumps(preview_data)
        )
        
        # Create ImportLog record with status='processing'
        import_log = ImportLog(
            id=UUID(import_id),
            gym_id=gym_id,
            filename=filename,
            status=ImportStatus.PROCESSING,
            total_rows=len(rows),
            success_count=0,
            error_count=0,
            errors_payload=[],
            created_by=created_by,
            created_at=datetime.now(timezone.utc)
        )
        self.session.add(import_log)
        await self.session.commit()
        
        # Sample first 5 rows (limit to 5)
        sample_rows = rows[:5]
        
        return {
            "import_id": import_id,
            "columns_detected": detected_columns,
            "row_count": len(rows),
            "sample_rows": sample_rows
        }

    async def confirm_import(
        self,
        import_id: str,
        gym_id: UUID,
        column_mapping: Dict[str, str],
        created_by: UUID
    ) -> Dict[str, Any]:
        """
        Execute bulk member import using stored preview data and column mapping.
        
        Args:
            import_id: UUID from preview step
            gym_id: Target gym UUID
            column_mapping: Mapping from CSV columns to model fields
                           e.g. {"member_name": "name", "phone": "phone"}
            created_by: Staff UUID performing import
            
        Returns:
            Dictionary with success_count, error_count, errors_list (optional)
        """
        # Load preview data from Redis
        preview_key = f"import_preview:{import_id}"
        preview_json = await redis_client.get(preview_key)
        if not preview_json:
            raise NotFoundError(f"Import session {import_id} not found or expired", error_code="NOT_FOUND")
        
        preview_data = json.loads(preview_json)
        
        # Verify gym matches
        if preview_data.get("gym_id") != str(gym_id):
            raise ValidationError("Gym ID mismatch", error_code="VALIDATION_ERROR")
        
        rows = preview_data.get("rows", [])
        if not rows:
            raise ValidationError("No rows to import", error_code="VALIDATION_ERROR")
        
        # Get import log record
        import_log = await self.session.get(ImportLog, UUID(import_id))
        if not import_log:
            raise NotFoundError(f"Import log {import_id} not found", error_code="NOT_FOUND")
        
        # Prepare mapping: CSV column -> model field
        # Required fields: member_name (maps to name), phone
        required_fields = ["member_name", "phone"]
        for req in required_fields:
            if req not in column_mapping:
                raise ValidationError(f"Missing mapping for required column: {req}", error_code="VALIDATION_ERROR")
        
        # Reverse mapping to know which CSV column corresponds to each model field
        # column_mapping: csv_column -> model_field
        # We need to build a dict model_field -> csv_column for easier extraction
        field_to_csv = {v: k for k, v in column_mapping.items()}
        
        success_count = 0
        error_count = 0
        errors_payload = []  # list of {row_index, error}
        
        # Process each row
        for idx, row in enumerate(rows, start=2):  # row 1 is header, so start at 2
            try:
                # Extract values using mapping
                name = row.get(field_to_csv.get("name", "member_name"), "").strip()
                phone_raw = row.get(field_to_csv.get("phone", "phone"), "").strip()
                
                if not name:
                    raise ValueError("Member name is required")
                if not phone_raw:
                    raise ValueError("Phone number is required")
                
                # Normalize phone
                try:
                    phone = normalize_phone(phone_raw)
                except ValueError as e:
                    raise ValueError(f"Invalid phone number: {str(e)}")
                
                # Check for duplicate phone in this gym
                existing = await self.member_repo.get_by_phone(phone, gym_id)
                if existing:
                    raise ValueError(f"Member with phone {phone} already exists in this gym")
                
                # Build MemberCreate schema
                member_data = MemberCreate(
                    name=name,
                    phone=phone,
                    email=row.get(field_to_csv.get("email", "email"), "").strip() or None,
                    date_of_birth=row.get(field_to_csv.get("date_of_birth", "date_of_birth"), "").strip() or None,
                    gender=row.get(field_to_csv.get("gender", "gender"), "").strip() or None,
                    blood_group=row.get(field_to_csv.get("blood_group", "blood_group"), "").strip() or None,
                    address=row.get(field_to_csv.get("address", "address"), "").strip() or None,
                    notes=row.get(field_to_csv.get("notes", "notes"), "").strip() or None
                )
                
                # Create member using service (handles limit, QR, etc.)
                await self.member_service.create_member(gym_id, member_data, created_by)
                success_count += 1
                
            except Exception as e:
                error_count += 1
                errors_payload.append({
                    "row": idx,
                    "data": row,
                    "error": str(e)
                })
                # Continue with next row
        
        # Update import log
        import_log.status = ImportStatus.COMPLETED if error_count == 0 else ImportStatus.PARTIAL
        import_log.success_count = success_count
        import_log.error_count = error_count
        import_log.errors_payload = errors_payload
        import_log.completed_at = datetime.now(timezone.utc)
        await self.session.commit()
        
        # Delete preview from Redis after confirmation
        await redis_client.delete(preview_key)
        
        logger.info(f"Import {import_id} completed: {success_count} success, {error_count} errors")
        
        return {
            "import_id": import_id,
            "success_count": success_count,
            "error_count": error_count,
            "errors": errors_payload[:100] if errors_payload else []  # limit for response
        }

    async def get_import_logs(
        self,
        gym_id: UUID,
        page: int = 1,
        size: int = 10
    ) -> Tuple[List[ImportLog], int]:
        """
        Get paginated import history for a gym.
        
        Returns:
            Tuple of (list of ImportLog, total count)
        """
        offset = (page - 1) * size
        
        query = select(ImportLog).where(
            ImportLog.gym_id == gym_id
        ).order_by(ImportLog.created_at.desc())
        
        count_query = select(func.count()).select_from(query.subquery())
        total_result = await self.session.execute(count_query)
        total = total_result.scalar() or 0
        
        query = query.offset(offset).limit(size)
        result = await self.session.execute(query)
        logs = result.scalars().all()
        
        return logs, total

    async def get_import_errors(
        self,
        import_id: str,
        gym_id: UUID
    ) -> str:
        """
        Get CSV string of error rows for an import log.
        
        Returns:
            CSV content as string (to be streamed as file download)
        """
        import_log = await self.session.get(ImportLog, UUID(import_id))
        if not import_log or import_log.gym_id != gym_id:
            raise NotFoundError(f"Import log {import_id} not found in gym {gym_id}", error_code="NOT_FOUND")
        
        errors = import_log.errors_payload
        if not errors:
            return "No errors found.\n"
        
        # Build CSV output
        output = StringIO()
        # Collect all possible columns from error rows
        all_columns = set()
        for err in errors:
            all_columns.update(err.get("data", {}).keys())
        columns = sorted(all_columns)
        columns.insert(0, "row_number")
        columns.append("error_message")
        
        writer = csv.DictWriter(output, fieldnames=columns)
        writer.writeheader()
        
        for err in errors:
            row_data = err.get("data", {}).copy()
            row_data["row_number"] = err.get("row")
            row_data["error_message"] = err.get("error")
            writer.writerow(row_data)
        
        return output.getvalue()

    async def get_template_csv(self) -> str:
        """
        Generate CSV template with expected columns.
        
        Returns:
            CSV content as string
        """
        output = StringIO()
        writer = csv.DictWriter(output, fieldnames=self.TEMPLATE_COLUMNS)
        writer.writeheader()
        # Add example row
        writer.writerow({
            "member_name": "John Doe",
            "phone": "9876543210",
            "email": "john@example.com",
            "date_of_birth": "1990-01-01",
            "gender": "MALE",
            "blood_group": "O+",
            "address": "123 Main St, City",
            "notes": "New member from import"
        })
        return output.getvalue()