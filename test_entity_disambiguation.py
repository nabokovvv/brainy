#!/usr/bin/env python3
"""
Test script for entity disambiguation bug fix.
Tests the Nicosia case from the design document.
"""

import asyncio
import httpx
import logging
import sys

# Setup logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Import the modules
import wikidata_mapper
import wikidata_fetcher

async def test_nicosia_russian():
    """Test the Russian genitive form of Nicosia"""
    logger.info("=" * 80)
    logger.info("Testing: Никосии (Nicosia in Russian, genitive case)")
    logger.info("=" * 80)
    
    async with httpx.AsyncClient() as client:
        # Test entity mapping
        qid = await wikidata_mapper.get_qid_from_entity(
            client, 
            "Никосии", 
            "ru", 
            spacy_label="LOC"
        )
        
        if qid:
            logger.info(f"\n✓ Result Q-ID: {qid}")
            
            # Fetch description and lead paragraph
            description = await wikidata_fetcher.get_wikidata_description(client, qid, "ru")
            lead_paragraph = await wikidata_fetcher.get_wikipedia_lead_paragraph(client, qid, "ru")
            
            logger.info(f"\n📋 Description: {description}")
            logger.info(f"\n📖 Lead Paragraph: {lead_paragraph[:200]}...")
            
            # Verify it's the city, not the theater
            if qid == "Q3856":
                logger.info(f"\n✅ SUCCESS: Correctly identified as Q3856 (Nicosia city)")
                return True
            elif qid == "Q18922613":
                logger.error(f"\n❌ FAILURE: Incorrectly identified as Q18922613 (theater)")
                return False
            else:
                logger.warning(f"\n⚠️  UNEXPECTED: Got Q-ID {qid} (neither city nor theater)")
                return False
        else:
            logger.error(f"\n❌ FAILURE: No Q-ID found")
            return False

async def test_nicosia_english():
    """Test English form of Nicosia"""
    logger.info("\n" + "=" * 80)
    logger.info("Testing: Nicosia (English)")
    logger.info("=" * 80)
    
    async with httpx.AsyncClient() as client:
        qid = await wikidata_mapper.get_qid_from_entity(
            client, 
            "Nicosia", 
            "en", 
            spacy_label="GPE"
        )
        
        if qid:
            logger.info(f"\n✓ Result Q-ID: {qid}")
            lead_paragraph = await wikidata_fetcher.get_wikipedia_lead_paragraph(client, qid, "en")
            logger.info(f"\n📖 Lead Paragraph: {lead_paragraph[:200]}...")
            
            if qid == "Q3856":
                logger.info(f"\n✅ SUCCESS: Correctly identified as Q3856 (Nicosia city)")
                return True
            else:
                logger.warning(f"\n⚠️  Got Q-ID {qid} (expected Q3856)")
                return False
        else:
            logger.error(f"\n❌ FAILURE: No Q-ID found")
            return False

async def test_paris():
    """Test Paris to ensure we don't break other location queries"""
    logger.info("\n" + "=" * 80)
    logger.info("Testing: Paris (should prefer city over commune)")
    logger.info("=" * 80)
    
    async with httpx.AsyncClient() as client:
        qid = await wikidata_mapper.get_qid_from_entity(
            client, 
            "Paris", 
            "en", 
            spacy_label="GPE"
        )
        
        if qid:
            logger.info(f"\n✓ Result Q-ID: {qid}")
            if qid == "Q90":
                logger.info(f"\n✅ SUCCESS: Correctly identified as Q90 (Paris city)")
                return True
            else:
                logger.warning(f"\n⚠️  Got Q-ID {qid} (expected Q90)")
                return False
        else:
            logger.error(f"\n❌ FAILURE: No Q-ID found")
            return False

async def main():
    """Run all tests"""
    results = []
    
    try:
        # Test 1: Russian Nicosia (the main bug fix)
        result1 = await test_nicosia_russian()
        results.append(("Nicosia (Russian)", result1))
        
        # Test 2: English Nicosia
        result2 = await test_nicosia_english()
        results.append(("Nicosia (English)", result2))
        
        # Test 3: Paris (regression check)
        result3 = await test_paris()
        results.append(("Paris (English)", result3))
        
    except Exception as e:
        logger.error(f"Test failed with exception: {e}", exc_info=True)
        return 1
    
    # Print summary
    logger.info("\n" + "=" * 80)
    logger.info("TEST SUMMARY")
    logger.info("=" * 80)
    
    for test_name, passed in results:
        status = "✅ PASS" if passed else "❌ FAIL"
        logger.info(f"{status}: {test_name}")
    
    passed_count = sum(1 for _, passed in results if passed)
    total_count = len(results)
    
    logger.info(f"\nResults: {passed_count}/{total_count} tests passed")
    
    return 0 if passed_count == total_count else 1

if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
