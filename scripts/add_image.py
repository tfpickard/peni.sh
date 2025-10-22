#!/usr/bin/env python3
"""
CLI tool for adding images to peni.sh with metadata.

Usage:
    python scripts/add_image.py <image_path> [options]

Examples:
    # Add a single image
    python scripts/add_image.py ~/pictures/cool.jpg

    # Add with category and tags
    python scripts/add_image.py ~/pictures/meme.png --category memes --tags funny viral

    # Add with full metadata
    python scripts/add_image.py ~/pictures/art.jpg --category art --tags abstract colorful --title "Abstract Art" --description "A beautiful abstract piece"

    # Add all images from a directory
    python scripts/add_image.py ~/pictures/ --category vacation --recursive
"""

import argparse
import shutil
import sys
from pathlib import Path
from typing import List, Optional

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import Config
from app.services.image_manager import image_manager


def add_single_image(
    source_path: Path,
    category: str = "default",
    tags: Optional[List[str]] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    copy: bool = True,
) -> bool:
    """Add a single image to the image directory.

    Args:
        source_path: Path to the source image
        category: Image category
        tags: List of tags
        title: Image title
        description: Image description
        copy: Whether to copy (True) or move (False) the file

    Returns:
        True if successful, False otherwise
    """
    try:
        # Validate image type
        if source_path.suffix.lower() not in Config.ALLOWED_IMAGE_TYPES:
            print(
                f"Error: {source_path.name} is not a supported image type. Supported: {Config.ALLOWED_IMAGE_TYPES}"
            )
            return False

        # Create category directory if needed
        image_dir = Path(Config.IMAGE_DIR)
        category_dir = image_dir / category
        category_dir.mkdir(parents=True, exist_ok=True)

        # Determine destination path
        dest_path = category_dir / source_path.name

        # Check if file already exists
        if dest_path.exists():
            response = input(
                f"{dest_path.name} already exists. Overwrite? [y/N]: "
            ).lower()
            if response != "y":
                print("Skipped.")
                return False

        # Copy or move the file
        if copy:
            shutil.copy2(source_path, dest_path)
            print(f"Copied {source_path.name} to {category}/")
        else:
            shutil.move(str(source_path), dest_path)
            print(f"Moved {source_path.name} to {category}/")

        # Add metadata
        relative_path = f"{category}/{source_path.name}"
        image_manager.add_image_metadata(
            filename=relative_path,
            category=category,
            tags=tags or [],
            title=title,
            description=description,
        )

        print(f"Added metadata: category={category}, tags={tags or []}")
        return True

    except Exception as e:
        print(f"Error adding {source_path.name}: {e}")
        return False


def add_images_from_directory(
    source_dir: Path,
    category: str = "default",
    tags: Optional[List[str]] = None,
    recursive: bool = False,
    copy: bool = True,
) -> tuple[int, int]:
    """Add all images from a directory.

    Args:
        source_dir: Path to source directory
        category: Image category
        tags: List of tags
        recursive: Whether to scan subdirectories
        copy: Whether to copy (True) or move (False) files

    Returns:
        Tuple of (successful_count, failed_count)
    """
    successful = 0
    failed = 0

    # Get image files
    pattern = "**/*" if recursive else "*"
    for file_path in source_dir.glob(pattern):
        if (
            file_path.is_file()
            and file_path.suffix.lower() in Config.ALLOWED_IMAGE_TYPES
        ):
            if add_single_image(file_path, category, tags, copy=copy):
                successful += 1
            else:
                failed += 1

    return successful, failed


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Add images to peni.sh with metadata",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    parser.add_argument("path", type=Path, help="Path to image file or directory")

    parser.add_argument(
        "-c",
        "--category",
        default="default",
        help="Image category (default: default)",
    )

    parser.add_argument(
        "-t", "--tags", nargs="+", help="Tags for the image (space-separated)"
    )

    parser.add_argument("--title", help="Image title")

    parser.add_argument("--description", help="Image description")

    parser.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="Recursively add images from subdirectories",
    )

    parser.add_argument(
        "-m",
        "--move",
        action="store_true",
        help="Move files instead of copying (default: copy)",
    )

    parser.add_argument(
        "--list-categories", action="store_true", help="List existing categories"
    )

    args = parser.parse_args()

    # List categories if requested
    if args.list_categories:
        categories = image_manager.list_categories()
        if categories:
            print("Existing categories:")
            for cat in categories:
                images = image_manager.get_images_by_category(cat)
                print(f"  - {cat} ({len(images)} images)")
        else:
            print("No categories found.")
        return

    # Validate path
    if not args.path.exists():
        print(f"Error: Path {args.path} does not exist.")
        sys.exit(1)

    print(f"peni.sh Image Manager")
    print(f"=" * 50)
    print(f"Image directory: {Config.IMAGE_DIR}")
    print(f"Category: {args.category}")
    print(f"Tags: {args.tags or 'none'}")
    print(f"Action: {'move' if args.move else 'copy'}")
    print(f"=" * 50)
    print()

    # Process path
    if args.path.is_file():
        # Single file
        success = add_single_image(
            args.path,
            args.category,
            args.tags,
            args.title,
            args.description,
            copy=not args.move,
        )
        if success:
            print("\nSuccess! Image added.")
            print("\nNext steps:")
            print("  - Restart the application to refresh the cache")
            print("  - Visit https://peni.sh to see your image")
        sys.exit(0 if success else 1)

    elif args.path.is_dir():
        # Directory
        successful, failed = add_images_from_directory(
            args.path,
            args.category,
            args.tags,
            args.recursive,
            copy=not args.move,
        )

        print(f"\n" + "=" * 50)
        print(f"Results:")
        print(f"  Successfully added: {successful}")
        print(f"  Failed: {failed}")
        print(f"=" * 50)

        if successful > 0:
            print("\nNext steps:")
            print("  - Restart the application to refresh the cache")
            print("  - Visit https://peni.sh to see your images")

        sys.exit(0 if failed == 0 else 1)

    else:
        print(f"Error: {args.path} is neither a file nor a directory.")
        sys.exit(1)


if __name__ == "__main__":
    main()
