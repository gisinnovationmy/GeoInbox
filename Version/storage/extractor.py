"""
GeoInbox - Extractor

Zip extraction and file validation utilities.
Handles nested zips and shapefile completeness validation.
"""

import zipfile
import os
import tempfile
import shutil
from pathlib import Path
from typing import List, Tuple, Optional, Set
from dataclasses import dataclass

from .sanitizer import sanitize_filename, resolve_collision, set_user_only_permissions
from ..utils.errors import StorageError, StorageErrorCode


ALLOWED_TOP_LEVEL_EXTENSIONS = {'.zip', '.geojson', '.gpkg', '.kml'}
ALLOWED_INNER_EXTENSIONS = {'.geojson', '.gpkg', '.kml', '.shp', '.shx', '.dbf', '.prj', '.cpg'}
SHAPEFILE_REQUIRED = {'.shp', '.dbf', '.shx'}
SHAPEFILE_OPTIONAL = {'.prj', '.cpg'}


@dataclass
class ExtractedFile:
    """Information about an extracted file."""
    original_name: str
    sanitized_name: str
    path: Path
    size: int
    is_valid: bool
    validation_message: str = ''


@dataclass
class ExtractionResult:
    """Result of an extraction operation."""
    success: bool
    extracted_files: List[ExtractedFile]
    rejected_files: List[Tuple[str, str]]  # (filename, reason)
    errors: List[str]


def is_allowed_extension(filename: str, top_level: bool = True) -> bool:
    """
    Check if a file extension is allowed.
    
    Args:
        filename: The filename to check
        top_level: Whether this is a top-level attachment
        
    Returns:
        True if the extension is allowed
    """
    ext = os.path.splitext(filename.lower())[1]
    if top_level:
        return ext in ALLOWED_TOP_LEVEL_EXTENSIONS
    return ext in ALLOWED_INNER_EXTENSIONS


def validate_shapefile_set(files: List[Path]) -> Tuple[bool, str]:
    """
    Validate that a shapefile set is complete.
    
    Args:
        files: List of file paths in the set
        
    Returns:
        Tuple of (is_valid, message)
    """
    extensions = {f.suffix.lower() for f in files}
    
    missing = SHAPEFILE_REQUIRED - extensions
    if missing:
        return False, f"Missing required shapefile components: {', '.join(missing)}"
    
    return True, "Shapefile set is complete"


def extract_zip(
    zip_path: Path,
    output_dir: Path,
    recursive: bool = True,
    validate_shapefiles: bool = True
) -> ExtractionResult:
    """
    Extract a ZIP file with validation.
    
    Args:
        zip_path: Path to the ZIP file
        output_dir: Directory to extract to
        recursive: Whether to recursively extract nested ZIPs
        validate_shapefiles: Whether to validate shapefile completeness
        
    Returns:
        ExtractionResult with details of the operation
    """
    extracted_files = []
    rejected_files = []
    errors = []
    
    if not zip_path.exists():
        return ExtractionResult(
            success=False,
            extracted_files=[],
            rejected_files=[],
            errors=[f"ZIP file not found: {zip_path}"]
        )
    
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            # First pass: categorize files
            nested_zips = []
            shapefile_groups = {}  # base_name -> list of extensions
            regular_files = []
            
            for info in zf.infolist():
                if info.is_dir():
                    continue
                
                filename = os.path.basename(info.filename)
                ext = os.path.splitext(filename.lower())[1]
                
                if ext == '.zip':
                    nested_zips.append(info)
                elif ext in ALLOWED_INNER_EXTENSIONS:
                    if ext in SHAPEFILE_REQUIRED | SHAPEFILE_OPTIONAL:
                        # Group shapefile components
                        base_name = os.path.splitext(filename)[0].lower()
                        if base_name not in shapefile_groups:
                            shapefile_groups[base_name] = []
                        shapefile_groups[base_name].append((info, ext))
                    else:
                        regular_files.append(info)
                else:
                    rejected_files.append((filename, f"Unsupported file type: {ext}"))
            
            # Process regular files
            for info in regular_files:
                try:
                    result = _extract_single_file(zf, info, output_dir)
                    extracted_files.append(result)
                except Exception as e:
                    errors.append(f"Failed to extract {info.filename}: {e}")
            
            # Process shapefile groups
            for base_name, components in shapefile_groups.items():
                extensions = {ext for _, ext in components}
                
                if validate_shapefiles:
                    missing = SHAPEFILE_REQUIRED - extensions
                    if missing:
                        for info, ext in components:
                            rejected_files.append((
                                os.path.basename(info.filename),
                                f"Incomplete shapefile set, missing: {', '.join(missing)}"
                            ))
                        continue
                
                # Extract all components
                for info, ext in components:
                    try:
                        result = _extract_single_file(zf, info, output_dir)
                        result.validation_message = "Part of shapefile set"
                        extracted_files.append(result)
                    except Exception as e:
                        errors.append(f"Failed to extract {info.filename}: {e}")
            
            # Process nested ZIPs recursively
            if recursive:
                for info in nested_zips:
                    try:
                        # Extract to temp location
                        with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as tmp:
                            tmp.write(zf.read(info.filename))
                            tmp_path = Path(tmp.name)
                        
                        # Recursively extract
                        nested_result = extract_zip(
                            tmp_path,
                            output_dir,
                            recursive=True,
                            validate_shapefiles=validate_shapefiles
                        )
                        
                        extracted_files.extend(nested_result.extracted_files)
                        rejected_files.extend(nested_result.rejected_files)
                        errors.extend(nested_result.errors)
                        
                        # Cleanup temp file
                        os.unlink(tmp_path)
                        
                    except Exception as e:
                        errors.append(f"Failed to extract nested ZIP {info.filename}: {e}")
        
        return ExtractionResult(
            success=len(errors) == 0,
            extracted_files=extracted_files,
            rejected_files=rejected_files,
            errors=errors
        )
        
    except zipfile.BadZipFile:
        return ExtractionResult(
            success=False,
            extracted_files=[],
            rejected_files=[],
            errors=["Invalid or corrupted ZIP file"]
        )
    except Exception as e:
        return ExtractionResult(
            success=False,
            extracted_files=[],
            rejected_files=[],
            errors=[f"Extraction failed: {e}"]
        )


def _extract_single_file(
    zf: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    output_dir: Path
) -> ExtractedFile:
    """Extract a single file from a ZIP."""
    original_name = os.path.basename(info.filename)
    sanitized_name = sanitize_filename(original_name)
    
    output_path = output_dir / sanitized_name
    output_path = resolve_collision(output_path)
    
    # Ensure output directory exists
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Extract
    with zf.open(info) as src, open(output_path, 'wb') as dst:
        shutil.copyfileobj(src, dst)
    
    # Set permissions
    set_user_only_permissions(output_path)
    
    return ExtractedFile(
        original_name=original_name,
        sanitized_name=output_path.name,
        path=output_path,
        size=info.file_size,
        is_valid=True
    )


def validate_gis_file(file_path: Path) -> Tuple[bool, str]:
    """
    Validate a GIS file by checking its format.
    
    Args:
        file_path: Path to the file
        
    Returns:
        Tuple of (is_valid, message)
    """
    ext = file_path.suffix.lower()
    
    if ext == '.geojson':
        return _validate_geojson(file_path)
    elif ext == '.gpkg':
        return _validate_geopackage(file_path)
    elif ext == '.kml':
        return _validate_kml(file_path)
    elif ext == '.shp':
        return _validate_shapefile(file_path)
    else:
        return False, f"Unknown file type: {ext}"


def _validate_geojson(file_path: Path) -> Tuple[bool, str]:
    """Validate a GeoJSON file."""
    try:
        import json
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        if 'type' not in data:
            return False, "Missing 'type' field"
        
        valid_types = {'FeatureCollection', 'Feature', 'Point', 'LineString', 
                       'Polygon', 'MultiPoint', 'MultiLineString', 'MultiPolygon',
                       'GeometryCollection'}
        
        if data['type'] not in valid_types:
            return False, f"Invalid GeoJSON type: {data['type']}"
        
        return True, "Valid GeoJSON"
        
    except json.JSONDecodeError as e:
        return False, f"Invalid JSON: {e}"
    except Exception as e:
        return False, f"Validation error: {e}"


def _validate_geopackage(file_path: Path) -> Tuple[bool, str]:
    """Validate a GeoPackage file."""
    try:
        import sqlite3
        conn = sqlite3.connect(str(file_path))
        cursor = conn.cursor()
        
        # Check for gpkg_contents table
        cursor.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name='gpkg_contents'
        """)
        
        if not cursor.fetchone():
            conn.close()
            return False, "Not a valid GeoPackage (missing gpkg_contents table)"
        
        # Get layer count
        cursor.execute("SELECT COUNT(*) FROM gpkg_contents")
        count = cursor.fetchone()[0]
        
        conn.close()
        return True, f"Valid GeoPackage with {count} layer(s)"
        
    except sqlite3.Error as e:
        return False, f"Database error: {e}"
    except Exception as e:
        return False, f"Validation error: {e}"


def _validate_kml(file_path: Path) -> Tuple[bool, str]:
    """Validate a KML file."""
    try:
        try:
            import defusedxml.ElementTree as ET
        except ImportError:
            import xml.etree.ElementTree as ET  # nosec B405
        tree = ET.parse(file_path)  # nosec B314
        root = tree.getroot()
        
        # Check for KML namespace
        if 'kml' not in root.tag.lower():
            return False, "Not a valid KML file"
        
        return True, "Valid KML"
        
    except ET.ParseError as e:
        return False, f"Invalid XML: {e}"
    except Exception as e:
        return False, f"Validation error: {e}"


def _validate_shapefile(file_path: Path) -> Tuple[bool, str]:
    """Validate a shapefile by checking for required components."""
    base_path = file_path.with_suffix('')
    
    required_files = [
        base_path.with_suffix('.shp'),
        base_path.with_suffix('.dbf'),
        base_path.with_suffix('.shx')
    ]
    
    missing = [f.suffix for f in required_files if not f.exists()]
    
    if missing:
        return False, f"Missing required components: {', '.join(missing)}"
    
    # Check if .prj exists
    prj_path = base_path.with_suffix('.prj')
    if not prj_path.exists():
        return True, "Valid shapefile (warning: missing .prj - CRS undefined)"
    
    return True, "Valid shapefile"


def get_file_preview(file_path: Path, max_chars: int = 1000) -> str:
    """
    Get a text preview of a GIS file.
    
    Args:
        file_path: Path to the file
        max_chars: Maximum characters to return
        
    Returns:
        Preview text
    """
    ext = file_path.suffix.lower()
    
    if ext in {'.geojson', '.kml'}:
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read(max_chars)
                if len(content) == max_chars:
                    content += '\n... (truncated)'
                return content
        except Exception as e:
            return f"Error reading file: {e}"
    
    elif ext == '.gpkg':
        try:
            import sqlite3
            conn = sqlite3.connect(str(file_path))
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT table_name, data_type, identifier 
                FROM gpkg_contents
            """)
            
            layers = cursor.fetchall()
            conn.close()
            
            if not layers:
                return "Empty GeoPackage"
            
            preview = "GeoPackage Layers:\n"
            for table_name, data_type, identifier in layers:
                preview += f"  - {table_name} ({data_type})\n"
            
            return preview
            
        except Exception as e:
            return f"Error reading GeoPackage: {e}"
    
    elif ext == '.shp':
        try:
            base_path = file_path.with_suffix('')
            
            preview = f"Shapefile: {file_path.stem}\n"
            preview += "Components:\n"
            
            for suffix in ['.shp', '.shx', '.dbf', '.prj', '.cpg']:
                component = base_path.with_suffix(suffix)
                if component.exists():
                    size = component.stat().st_size
                    preview += f"  - {suffix}: {size} bytes\n"
            
            return preview
            
        except Exception as e:
            return f"Error reading shapefile: {e}"
    
    return "Preview not available for this file type"
