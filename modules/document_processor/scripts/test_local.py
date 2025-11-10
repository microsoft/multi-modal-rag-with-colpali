#!/usr/bin/env python3
"""
Local testing script for the Document Processor
Tests the document processing pipeline without requiring Service Bus, QDRANT, or ColPali endpoints
Uses local mode to bypass production environment variable requirements
"""

import asyncio
import logging
import os
import sys
import time

from dotenv import find_dotenv, load_dotenv

# Find and load .env file from the project root
load_dotenv(find_dotenv())

# Add the src directory to the path so we can import the processing modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def setup_logging():
    """Setup logging for the local test"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )


async def process_file_local(file_path: str) -> bool:
    """
    Process a PDF file locally using the existing process_document_async function

    Args:
        file_path: Path to the PDF file to process

    Returns:
        True if successful, False otherwise
    """
    logger = logging.getLogger(__name__)

    if not os.path.exists(file_path):
        logger.error(f"File not found: {file_path}")
        return False

    if not file_path.lower().endswith(".pdf"):
        logger.error(f"Only PDF files are supported. Got: {file_path}")
        return False

    file_size = os.path.getsize(file_path)
    filename = os.path.basename(file_path)

    logger.info(f"Processing: {file_path}")
    logger.info(f"File size: {file_size:,} bytes")

    # Initialize start_time before try block to ensure it's always available
    start_time = time.time()

    try:
        # Import the consolidated DocumentProcessor
        from document_processor import DocumentProcessor

        # Read the file
        with open(file_path, "rb") as f:
            file_content = f.read()

        logger.info(f"File loaded: {len(file_content):,} bytes")

        # Initialize the document processor in local mode (no Service Bus required)
        processor = DocumentProcessor(require_service_bus=False)

        logger.info("Document processor initialized successfully (local mode)")

        # Process the document using the complete pipeline
        result = await processor.process_document_complete(
            blob_content=file_content, blob_name=filename, file_extension=".pdf"
        )

        end_time = time.time()
        total_processing_time = end_time - start_time

        if result.success:
            logger.info("Processing completed successfully!")
            logger.info(f"Total time: {total_processing_time:.2f} seconds")
            logger.info(
                f"Success rate: {result.success_rate:.1f}% ({result.pages_processed}/{result.total_pages} pages)"
            )
        else:
            logger.error("Processing failed - check logs above for details")
            if result.error_message:
                logger.error(f"Error: {result.error_message}")

        return result.success

    except ImportError as e:
        logger.error(f"Import error: {str(e)}")
        logger.error(
            "Make sure you're running from the document_processor directory and all dependencies are installed"
        )
        return False
    except Exception as e:
        end_time = time.time()
        processing_time = end_time - start_time
        logger.error(
            f"Error processing document after {processing_time:.2f} seconds: {str(e)}"
        )
        return False


async def process_all_test_files():
    """
    Process all PDF files in the test_files directory

    Returns:
        True if all files processed successfully, False otherwise
    """
    logger = logging.getLogger(__name__)

    # Get the test_files directory path
    script_dir = os.path.dirname(__file__)
    test_files_dir = os.path.join(script_dir, "..", "test_files")
    test_files_dir = os.path.abspath(test_files_dir)

    if not os.path.exists(test_files_dir):
        logger.error(f"Test files directory not found: {test_files_dir}")
        logger.info("Create the directory and add some PDF files to test")
        return False

    # Find all PDF files
    pdf_files = []
    for file in os.listdir(test_files_dir):
        if file.lower().endswith(".pdf"):
            pdf_files.append(os.path.join(test_files_dir, file))

    if not pdf_files:
        logger.error(f"No PDF files found in: {test_files_dir}")
        logger.info("Add some PDF files to the test_files directory")
        return False

    logger.info(f"Found {len(pdf_files)} PDF file(s) in test_files directory:")
    for pdf_file in pdf_files:
        logger.info(f"  - {os.path.basename(pdf_file)}")

    # Process each file
    successful_files = 0
    total_files = len(pdf_files)

    for i, pdf_file in enumerate(pdf_files, 1):
        logger.info(f"Processing file {i}/{total_files}: {os.path.basename(pdf_file)}")
        logger.info("-" * 60)

        try:
            success = await process_file_local(pdf_file)
            if success:
                successful_files += 1
                logger.info(f"Successfully processed: {os.path.basename(pdf_file)}")
            else:
                logger.error(f"Failed to process: {os.path.basename(pdf_file)}")
        except Exception as e:
            logger.error(f"Error processing {os.path.basename(pdf_file)}: {str(e)}")

    # Summary
    logger.info("=" * 60)
    logger.info("Processing Summary:")
    logger.info(f"  - Total files: {total_files}")
    logger.info(f"  - Successful: {successful_files}")
    logger.info(f"  - Failed: {total_files - successful_files}")

    if successful_files == total_files:
        logger.info("All files processed successfully!")
        return True
    else:
        logger.warning(f"{total_files - successful_files} file(s) failed processing")
        return False


def main():
    # Setup logging
    setup_logging()

    logger = logging.getLogger(__name__)
    logger.info("Document Processor Local Tester")
    logger.info("=" * 40)

    # Just process all files in test_files directory
    success = asyncio.run(process_all_test_files())
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
