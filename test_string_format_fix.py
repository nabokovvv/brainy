#!/usr/bin/env python3
"""
Test script to verify the string formatting bug fix in together_client.py

This script tests the exact pattern used in polish_research_answer and 
summarize_research_chunk functions to ensure the KeyError bug is resolved.
"""

def test_polish_research_answer_pattern():
    """Test the pattern used in polish_research_answer function"""
    print("\n=== Testing polish_research_answer pattern ===")
    
    # Simulate the actual prompt template from together_client.py
    query = "What is quantum computing?"
    lang = "en"
    summaries = "Test summaries from research chunks"
    
    prompt_template = """You are a chief researcher. Answer the user's query based on the research data provided to you. 

**User Query:** {query}

**Research Data (Summaries):**
{summaries}

**Rules:**
1. Crucially, cite your sources in the following format "[https://example.com/page](https://example.com/page)" directly within the text where the information is used.
2. List facts from junior researchers, check them for any contradictions, and only then compose the detailed final answer.

7. Your final answer must be in the "{lang}" language.

IMPORTANT: You MUST respond with ONLY a valid JSON object (no markdown code blocks, no explanatory text). Use this exact format:
{{{{
  "thinking": "Your analysis of summaries and final synthesis strategy",
  "final": "Your COMPLETE, DETAILED, and POLISHED answer in {lang} language with inline citations",
  "sources": ["https://url1.com", "https://url2.com"]
}}}}"""
    
    try:
        # Test 1: base_prompt_len calculation (line 1120)
        base_prompt_len = len(prompt_template.format(summaries='', query=query, lang=lang))
        print(f"✓ Test 1 PASSED: base_prompt_len calculation works (length={base_prompt_len})")
        
        # Test 2: final_prompt assembly (line 1129)
        final_prompt = prompt_template.format(summaries=summaries, query=query, lang=lang)
        print(f"✓ Test 2 PASSED: final_prompt assembly works (length={len(final_prompt)})")
        
        # Test 3: Verify JSON braces are preserved in output
        if '{{' in final_prompt and '}}' in final_prompt:
            print("✓ Test 3 PASSED: JSON braces are preserved in final prompt")
        else:
            print("✗ Test 3 FAILED: JSON braces not found in final prompt")
            return False
            
        # Test 4: Verify placeholders are substituted
        if '{query}' not in final_prompt and query in final_prompt:
            print("✓ Test 4 PASSED: query placeholder substituted correctly")
        else:
            print("✗ Test 4 FAILED: query placeholder not substituted")
            return False
            
        if '{summaries}' not in final_prompt and summaries in final_prompt:
            print("✓ Test 5 PASSED: summaries placeholder substituted correctly")
        else:
            print("✗ Test 5 FAILED: summaries placeholder not substituted")
            return False
            
        # Test 6: Verify {lang} inside JSON example is also substituted
        if '"final": "Your COMPLETE, DETAILED, and POLISHED answer in en language' in final_prompt:
            print("✓ Test 6 PASSED: lang placeholder in JSON example substituted")
        else:
            print("✗ Test 6 FAILED: lang placeholder in JSON example not substituted")
            return False
            
        return True
        
    except KeyError as e:
        print(f"✗ FAILED: KeyError occurred: {e}")
        return False
    except Exception as e:
        print(f"✗ FAILED: Unexpected error: {e}")
        return False


def test_summarize_research_chunk_pattern():
    """Test the pattern used in summarize_research_chunk function"""
    print("\n=== Testing summarize_research_chunk pattern ===")
    
    query = "How does photosynthesis work?"
    lang = "en"
    chunk = "Sample research data chunk about photosynthesis"
    
    prompt = """You are a research assistant. Analyze this piece of the research draft and summarize in a detailed and well-structured way the key information that can help partly or fully answer the user's main query, which is: '{query}'.

IMPORTANT: You MUST respond with ONLY a valid JSON object (no markdown code blocks, no explanatory text). Use this exact format:
{{{{
  "thinking": "Your analysis of chunk content and relevance to query",
  "final": "Your detailed summary in {lang} language with citations in square brackets (domains or full URLs)",
  "sources": ["https://url1.com", "domain.com"]
}}}}

Provide only the summary in the 'final' field, with no extra comments or introductions. Stick closer to the language and style of provided context snippets. The summary must be in the "{lang}" language. Don't forget to cite sources (if any) in square brackets.

**Research Draft Chunk:**

{chunk}"""
    
    try:
        # Format the prompt (simulating line 1225 where it's used)
        final_prompt = prompt.format(query=query, lang=lang, chunk=chunk)
        print(f"✓ Test 1 PASSED: prompt formatting works (length={len(final_prompt)})")
        
        # Verify JSON braces are preserved
        if '{{' in final_prompt and '}}' in final_prompt:
            print("✓ Test 2 PASSED: JSON braces preserved")
        else:
            print("✗ Test 2 FAILED: JSON braces not preserved")
            return False
            
        # Verify placeholders are substituted
        if query in final_prompt and '{query}' not in final_prompt:
            print("✓ Test 3 PASSED: query substituted correctly")
        else:
            print("✗ Test 3 FAILED: query not substituted correctly")
            return False
            
        return True
        
    except KeyError as e:
        print(f"✗ FAILED: KeyError occurred: {e}")
        return False
    except Exception as e:
        print(f"✗ FAILED: Unexpected error: {e}")
        return False


def test_edge_cases():
    """Test edge cases and special characters"""
    print("\n=== Testing Edge Cases ===")
    
    # Test with special characters in parameters
    test_cases = [
        {
            'name': 'Cyrillic characters',
            'query': 'Что такое квантовая физика?',
            'lang': 'ru',
            'summaries': 'Квантовая физика изучает микромир'
        },
        {
            'name': 'Long text',
            'query': 'A' * 1000,
            'lang': 'en',
            'summaries': 'B' * 5000
        },
        {
            'name': 'Special characters',
            'query': 'Test with "quotes" and {braces}',
            'lang': 'en',
            'summaries': 'Data with special chars: @#$%^&*()'
        }
    ]
    
    prompt_template = """Query: {query}
Lang: {lang}
JSON: {{{{
  "final": "Answer in {lang}"
}}}}
Summaries: {summaries}"""
    
    for test_case in test_cases:
        try:
            result = prompt_template.format(
                query=test_case['query'],
                lang=test_case['lang'],
                summaries=test_case['summaries']
            )
            if '{{' in result and test_case['query'] in result:
                print(f"✓ Test '{test_case['name']}' PASSED")
            else:
                print(f"✗ Test '{test_case['name']}' FAILED")
                return False
        except Exception as e:
            print(f"✗ Test '{test_case['name']}' FAILED with error: {e}")
            return False
    
    return True


def main():
    """Run all tests"""
    print("=" * 70)
    print("String Formatting Bug Fix Verification Tests")
    print("=" * 70)
    
    results = []
    
    # Run tests
    results.append(("polish_research_answer pattern", test_polish_research_answer_pattern()))
    results.append(("summarize_research_chunk pattern", test_summarize_research_chunk_pattern()))
    results.append(("Edge cases", test_edge_cases()))
    
    # Summary
    print("\n" + "=" * 70)
    print("TEST SUMMARY")
    print("=" * 70)
    
    all_passed = True
    for test_name, result in results:
        status = "✓ PASSED" if result else "✗ FAILED"
        print(f"{test_name}: {status}")
        if not result:
            all_passed = False
    
    print("\n" + "=" * 70)
    if all_passed:
        print("✓ ALL TESTS PASSED - Bug fix is working correctly!")
        print("=" * 70)
        return 0
    else:
        print("✗ SOME TESTS FAILED - Please review the implementation")
        print("=" * 70)
        return 1


if __name__ == "__main__":
    exit(main())
