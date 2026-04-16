"""
ftp_service.py
Polls the shared FTP server for new POD files and matches them to delivery numbers.
"""
import ftplib
import io
import os
import logging
import tempfile
from datetime import datetime, timezone
from typing import Optional, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.models import SystemConfig, PodRegistry, Carrier
from app.services.pod_folder_service import save_pod_bytes

logger = logging.getLogger(__name__)

# Track which FTP files have already been processed (in-memory; survives restarts via registry)
_processed_files = set()


async def _get_ftp_config(db: AsyncSession) -> dict:
    keys = ['ftp_host', 'ftp_user', 'ftp_password', 'ftp_base_path', 'ftp_poll_interval_minutes']
    result = await db.execute(select(SystemConfig).where(SystemConfig.key.in_(keys)))
    return {r.key: r.value for r in result.scalars().all()}


async def get_pending_delivery_numbers(db: AsyncSession) -> List[str]:
    """Get all delivery numbers that are pending or requested (no POD yet)."""
    result = await db.execute(
        select(PodRegistry).where(
            PodRegistry.status.in_(['pending', 'requested'])
        )
    )
    return [r.delivery_number for r in result.scalars().all()]


async def ai_match_filename_to_delivery(
    filename: str,
    pending_delivery_numbers: List[str],
    ollama_base_url: str,
    model: str,
) -> Optional[str]:
    """
    Use the LLM to try to match a PDF filename to a delivery number.
    Returns matched delivery number or None.
    """
    import httpx
    if not pending_delivery_numbers:
        return None

    prompt = f"""You are matching a PDF filename to a delivery number.

Filename: {filename}

Pending delivery numbers that need PODs:
{chr(10).join(pending_delivery_numbers)}

Does this filename likely correspond to one of these delivery numbers?
Look for partial matches, numeric sequences, or any identifiers in common.

Respond with ONLY the matching delivery number if confident (e.g. "DEL-2024-0881"),
or "NO_MATCH" if you cannot confidently match it.
No explanation needed."""

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{ollama_base_url}/api/generate",
                json={"model": model, "prompt": prompt, "stream": False}
            )
            response = r.json().get('response', '').strip()
            if response and response != 'NO_MATCH':
                # Verify the returned value is actually in our list
                for dn in pending_delivery_numbers:
                    if dn.lower() in response.lower() or response.lower() in dn.lower():
                        return dn
    except Exception as e:
        logger.error(f"AI filename matching failed: {e}")
    return None


async def poll_ftp(db: AsyncSession, ollama_base_url: str, model: str) -> dict:
    """
    Poll the FTP server for new PDF files.
    Try to match each to a pending delivery number and save to POD folder.
    Returns summary of what was found/matched.
    """
    cfg = await _get_ftp_config(db)
    host = cfg.get('ftp_host', '')
    user = cfg.get('ftp_user', '')
    password = cfg.get('ftp_password', '')
    base_path = cfg.get('ftp_base_path', '/pods')

    if not host or not user:
        return {'status': 'skipped', 'reason': 'FTP not configured'}

    pending_dns = await get_pending_delivery_numbers(db)
    results = {'scanned': 0, 'matched': 0, 'unmatched': 0, 'errors': 0, 'files': []}

    try:
        ftp = ftplib.FTP(timeout=30)
        ftp.connect(host)
        ftp.login(user, password)

        # Get all carrier subfolders + base path
        paths_to_scan = [base_path]
        result = await db.execute(
            select(Carrier).where(Carrier.uses_ftp == True, Carrier.is_active == True)
        )
        carriers = result.scalars().all()
        for carrier in carriers:
            if carrier.ftp_subfolder:
                paths_to_scan.append(
                    os.path.join(base_path, carrier.ftp_subfolder).replace('\\', '/')
                )

        for path in paths_to_scan:
            try:
                files = []
                ftp.retrlines(f'LIST {path}', files.append)
                for file_entry in files:
                    parts = file_entry.split()
                    if not parts:
                        continue
                    filename = parts[-1]
                    if not filename.lower().endswith('.pdf'):
                        continue

                    ftp_path = f"{path}/{filename}".replace('//', '/')
                    if ftp_path in _processed_files:
                        continue

                    results['scanned'] += 1

                    # Download file
                    buf = io.BytesIO()
                    try:
                        ftp.retrbinary(f'RETR {ftp_path}', buf.write)
                    except Exception as e:
                        logger.error(f"FTP download failed for {ftp_path}: {e}")
                        results['errors'] += 1
                        continue

                    file_bytes = buf.getvalue()

                    # Try AI matching
                    matched_dn = await ai_match_filename_to_delivery(
                        filename, pending_dns, ollama_base_url, model
                    )

                    file_info = {'filename': filename, 'ftp_path': ftp_path, 'matched_dn': matched_dn}

                    if matched_dn:
                        saved = await save_pod_bytes(
                            db=db,
                            file_bytes=file_bytes,
                            delivery_number=matched_dn,
                            original_filename=filename,
                            received_via='ftp',
                            matched_by='ai',
                        )
                        if saved:
                            _processed_files.add(ftp_path)
                            results['matched'] += 1
                            file_info['saved_as'] = saved
                            pending_dns = [d for d in pending_dns if d != matched_dn]
                            logger.info(f"FTP: matched {filename} → {matched_dn}")
                    else:
                        results['unmatched'] += 1
                        logger.warning(f"FTP: could not match {filename} to any pending delivery")

                    results['files'].append(file_info)

            except ftplib.error_perm as e:
                logger.warning(f"FTP path {path} not accessible: {e}")

        ftp.quit()

    except Exception as e:
        logger.error(f"FTP poll failed: {e}")
        results['error'] = str(e)
        results['status'] = 'error'
        return results

    results['status'] = 'ok'
    return results
