from typing import List, Literal
from pydantic import BaseModel, Field
from openai import OpenAI

import json

# structured output for style scoring
class StyleScoreResponse(BaseModel):
    systematic_evaluation_score: Literal[0, 0.5, 1]
    organization_language_score: Literal[0, 0.5, 1]

    systematic_evaluation_recommendation: str = Field(
        description="Recommendation for systematic evaluation (empty if score is 1)"
    )
    organization_language_recommendation: str = Field(
        description="Recommendation for organization and language (empty if score is 1)"
    )

def get_style_score(candidate_report: str, openai_client: OpenAI) -> StyleScoreResponse:

    prompt = f'''
    Objective:
    
    Evaluate the writing style and structure of a radiology report to determine how well it follows professional radiology reporting standards. Focus on style, structure, and systematic evaluation rather than clinical accuracy.
    
    Criteria for Judgment:
    
    Rate each aspect as 0 (poor), 0.5 (adequate), or 1 (excellent):
    
    1. SYSTEMATIC EVALUATION: Does the report cover the major chest X-ray regions?
       - 1.0: Covers most/all major areas (lungs, heart, bones, mediastinum) in organized way
       - 0.5: Covers several major areas but may miss 1-2 or lack organization
       - 0.0: Only mentions 1-2 areas or very disorganized
       
    2. ORGANIZATION AND LANGUAGE: Is the report reasonably well-organized and written in appropriate clinical language?
       - 1.0: Clear organization with, complete sentences, clinical language
       - 0.5: Some organization present, mostly complete sentences
       - 0.0: Poor organization, incomplete sentences, non-clinical language

    Candidate Report:
    {candidate_report}
    

    NOTES: 
        - Do NOT recommend the user to make sections or sub-sections in the report such as Findings, Impression, etc. 
        - Provide 1 recommendation per scoring category that scored less than 1.0
        - If a category scores 1.0 (perfect), leave that recommendation field empty ("")
        - Keep each recommendation very concise and actionable

    Be concise in your recommendations.
    Provide your assessment in the following JSON format:
    {{
        "systematic_evaluation_score": <0, 0.5, or 1>,
        "organization_language_score": <0, 0.5, or 1>,
        "systematic_evaluation_recommendation": "<Recommendation if score < 1, otherwise empty>",
        "organization_language_recommendation": "<Recommendation if score < 1, otherwise empty>"
    }}
    '''
    
    completion = openai_client.chat.completions.create(
        model="o3",
        messages=[
            {
                "role": "system", 
                "content": "You are a radiology education expert that evaluates the writing style and structure of radiology reports.  Return only valid JSON."
            },
            {"role": "user", "content": prompt}
        ],
        response_format={ "type": "json_object" }
    )
    
    response_data = json.loads(completion.choices[0].message.content)
    return StyleScoreResponse(**response_data)


def calculate_style_score(candidate_report: str, openai_client: OpenAI):

    style_response = get_style_score(candidate_report, openai_client)

    return style_response, ((float(style_response.systematic_evaluation_score) + float(style_response.organization_language_score))/2.0)*100.0
