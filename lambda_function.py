"""
Salesforce Tasks Lambda Function
Provides CRUD operations for Salesforce Tasks using shared authentication utilities
"""

import json
import requests
import logging
import traceback
import os
import boto3
import json
import random
import urllib.parse
from datetime import datetime
from zoneinfo import ZoneInfo
from boto3.dynamodb.conditions import Key

# Import shared utilities (same pattern as your JS code)
from common.sf_auth import get_access_token, sf_query
from common.sf_utils import safe_json

DYNAMODB_TABLE = os.environ.get('INQUIRY_FORM_DYNAMODB_TABLE', 'InquiryFormData')
DYNAMODB_REGION = os.environ.get('INQUIRY_FORM_DYNAMODB_REGION', 'us-east-1')
dynamodb = boto3.resource('dynamodb', region_name=DYNAMODB_REGION)
attachments_table = dynamodb.Table(DYNAMODB_TABLE)
# Initialize AWS Connect client
connect_client = boto3.client('connect', region_name=DYNAMODB_REGION)

# Setup logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize AWS clients
pinpoint_client = boto3.client("pinpoint")
dynamodb_survey = boto3.resource("dynamodb")
ses_client = boto3.client("ses")
connect_client_survey = boto3.client("connect")
sesv2_client = boto3.client("sesv2")
SMS_CLIENT = boto3.client("pinpoint-sms-voice-v2")

# Environment variables
APPLICATION_ID = os.environ.get("PINPOINT_APP_ID")
ORIGINATION_NUMBER = os.environ.get("ORIGINATION_NUMBER")
OPT_OUT_TABLE = os.environ.get("OPT_OUT_TABLE")
INSTANCE_ID = os.environ.get("CONNECT_INSTANCE_ID")
SF_URL = os.environ.get("SALESFORCE_URL")
UNSUBSCRIBE_URL = os.getenv("UNSUBSCRIBE_URL")
SURVEY_S3_BUCKET_URL = os.getenv("SURVEY_S3_BUCKET_URL")

def is_opted_out(phone_number):
    """Check if customer has opted out using DynamoDB."""
    try:
        logger.info("Checking opt-out status for: %s", phone_number)
        table = dynamodb_survey.Table(OPT_OUT_TABLE)

        response = table.get_item(Key={"PhoneNumber": phone_number})
        logger.info("DynamoDB response: %s", response)

        item = response.get("Item")
        return item and item.get("opt-out") is True

    except Exception:
        logger.error("[is_opted_out]: %s", traceback.format_exc())
        return False


def get_email_template(template_name):
    try:
        response = sesv2_client.get_email_template(TemplateName=template_name)
        logger.info("Template content: %s", json.dumps(response, indent=2))
        return response
    except Exception as e:
        logger.error(f"Failed to get SES template: {e}")
        return None

def send_survey_sms(phone_number, caller_name, customer_email, task_number, contact_id, instance_id):
    """Send survey SMS and email after each inbound call, then insert SFDC record."""

    print(f"Sending survey SMS with details: {phone_number}, {caller_name}, {customer_email}, {task_number}, {contact_id}, {instance_id}")

    try:
        if contact_id:
            response = connect_client.describe_contact(
                InstanceId=instance_id, ContactId=contact_id
            )
            logger.info(f"Parsed response from describe_contact: {response}")
            agent_id = response["Contact"]["AgentInfo"]["Id"]
            agent_details = connect_client.describe_user(
                UserId=agent_id, InstanceId=instance_id
            )
            logger.info(f"Parsed response from agent_details: {agent_details}")
            agent_name = f"{agent_details['User']['IdentityInfo'].get('FirstName', '')} {agent_details['User']['IdentityInfo'].get('LastName', '')}"

        greeting = f"Hi {caller_name}," if caller_name.strip() else "Hi,"
        survey_format_sms = (
            f"{greeting} \n\n"
            "Thanks for choosing Qcells to power your clean energy journey! "
            "We'd love your feedback on your recent experience.\n"
            "Please tap the link below to share your thoughts:\n\n"
            f"{SURVEY_S3_BUCKET_URL}?id={contact_id}&channel=sms"
        )
        message = survey_format_sms + '\n\nReply "STOP" to opt-out.'

        if not phone_number or not message:
            return {
                "statusCode": 400,
                "body": json.dumps({"error": "Missing phone_number or message"}),
            }

        sms_sent = False
        email_sent = False

        # --- SEND SMS ---
        try:
            sms_response = SMS_CLIENT.send_text_message(
                DestinationPhoneNumber=phone_number,
                MessageBody=message,
                MessageType="TRANSACTIONAL",
                OriginationIdentity=ORIGINATION_NUMBER,
            )
            logger.info("SMS sent successfully: %s", sms_response)
            sms_sent = True
        except Exception as sms_exception:
            logger.error(f"Failed to send SMS: {sms_exception}")

        # --- SEND EMAIL (if not opted out) ---
        try:
            if not customer_email:
                raise ValueError("Missing customer email.")

            token_res = get_access_token()
            logger.info("Access token obtained.")

            object_api_name = "Survey_OPT_Out_List__c"
            fields = ["Survey_Opt_Out_Email__c", "NAME"]
            soql_query = f"SELECT {','.join(fields)} FROM {object_api_name} WHERE Survey_Opt_Out_Email__c='{customer_email}'"
            encoded_query = urllib.parse.quote_plus(soql_query, safe="=','")

            url = f"{token_res['instance_url']}/services/data/v64.0/query?q={encoded_query}"
            headers = {
                "Authorization": f"Bearer {token_res['access_token']}",
                "Content-Type": "application/json",
            }

            response = requests.get(url, headers=headers)
            if response.status_code != 200:
                raise Exception(f"Salesforce query failed: {response.text}")

            data = response.json()
            if not data.get("records"):
                template_data = {
                    "name": caller_name,
                    "contact_id": contact_id,
                    "unsubscribe_link": f"{UNSUBSCRIBE_URL}{contact_id}",
                }

                email_response = ses_client.send_templated_email(
                    Source="na.support@qcells.com",
                    Destination={"ToAddresses": [customer_email.lower()]},
                    ReplyToAddresses=["na.support@qcells.com"],
                    Template="Survey_Email_With_Unsubscribe_New",
                    TemplateData=json.dumps(template_data),
                )
                logger.info("Email sent successfully: %s", email_response)
                email_sent = True
            else:
                logger.info(f"Email opted out for {customer_email}")
        except Exception as email_exception:
            logger.error(f"Error sending email: {email_exception}")

        # --- INSERT RECORD TO SALESFORCE IF BOTH SENT ---
        if sms_sent and email_sent:
            try:
                # Get access token using shared function
                token_res = get_access_token()
                access_token = token_res['access_token']

                pst_datetime = datetime.now(ZoneInfo("America/Los_Angeles"))
                sent_at = pst_datetime.isoformat()

                insert_url = f"{token_res['instance_url']}/services/data/v64.0/sobjects/NPS_Survey_Sent__c"
                headers = {
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                }
                payload = {
                    "Agent_Name__c": agent_name,
                    "Email__c": customer_email,
                    "Phone_Number__c": phone_number,
                    "Related_Task_Number__c": task_number,
                    "Survey_Sent_Date__c": sent_at,
                }

                insert_response = requests.post(
                    insert_url, headers=headers, json=payload
                )
                if insert_response.status_code not in [200, 201]:
                    raise Exception(f"Insert failed: {insert_response.text}")

                logger.info("Inserted record into NPS_Survey_Sent__c")

            except Exception as insert_exception:
                logger.error(f"Failed to insert Salesforce record: {insert_exception}")

        return {
            "statusCode": 200,
            "body": "SMS and Email sent. Salesforce record insertion attempted.",
        }

    except Exception as e:
        logger.error(f"Unhandled error: {traceback.format_exc()}")
        return {"statusCode": 500, "body": "Unhandled server error."}


def get_contact_attributes(instance_id, contact_id):
    """
    Get contact attributes from AWS Connect

    Args:
        instance_id: AWS Connect instance ID
        contact_id: Contact ID (InitialContactId)

    Returns:
        Dictionary with contact attributes
    """
    try:
        response = connect_client.get_contact_attributes(
            InstanceId=instance_id, 
            InitialContactId=contact_id
        )
        contact_attributes = response['Attributes']
        print(f"contact_attributes {contact_attributes}")
        return contact_attributes # if contact does not give attributes

    except Exception as e:
        print(f"❌ Error fetching contact attributes: {str(e)}")
        raise

def create_task_detail(task_detail_data):
    """
    Create a new task detail in Salesforce

    Args:
        task_data: Dictionary with task fields (Subject, Status, Priority, etc.)

    Returns:
        Created task info from Salesforce
    """
    if not task_detail_data or 'Name' not in task_detail_data:
        raise ValueError("Task detail data with Task_Details_Name__c is required")
    
    # Get access token using shared function
    token_res = get_access_token()

    task_url = f"{token_res['instance_url']}/services/data/v59.0/sobjects/Task_Detail__c"

    try:
        response = requests.post(
            task_url,
            headers={
                'Authorization': f"Bearer {token_res['access_token']}",
                'Content-Type': 'application/json'
            },
            json=task_detail_data
        )

        if response.status_code != 201:
            error_data = safe_json(response.text)
            print(error_data)
            error_msg = error_data.get('message') if error_data else f"Failed to create task: {response.status_code}"
            raise Exception(error_msg)

        return response.json()
    except requests.exceptions.RequestException as e:
        raise Exception(f"Failed to create task: {str(e)}")

def update_task(task_id, task_data):
    """
    Update an existing task in Salesforce

    Args:
        task_id: Salesforce Task ID
        task_data: Dictionary with fields to update

    Returns:
        Update result
    """
    if not task_id:
        raise ValueError("Task ID is required")

    if not task_data or len(task_data) == 0:
        raise ValueError("Task data is required")

    # Get access token using shared function
    token_res = get_access_token()

    task_url = f"{token_res['instance_url']}/services/data/v59.0/sobjects/Task/{task_id}"
    print(task_url)
    try:
        response = requests.patch(
            task_url,
            headers={
                'Authorization': f"Bearer {token_res['access_token']}",
                'Content-Type': 'application/json'
            },
            json=task_data
        )
        print(response)
        print(response.status_code)
        print(response.text)
        if response.status_code != 204:
            error_data = safe_json(response.text)
            # Salesforce returns errors as a list of error objects
            if isinstance(error_data, list) and len(error_data) > 0:
                error_msg = error_data[0].get('message', f"Failed to update task: {response.status_code}")
            elif isinstance(error_data, dict):
                error_msg = error_data.get('message', f"Failed to update task: {response.status_code}")
            else:
                error_msg = f"Failed to update task: {response.status_code}"
            raise Exception(error_msg)

        return {'success': True, 'taskId': task_id}
    except requests.exceptions.RequestException as e:
        raise Exception(f"Failed to update task {task_id}: {str(e)}")

def update_connect_contact_attributes(instance_id, initial_contact_id, attributes):
    """
    Update AWS Connect contact attributes

    Args:
        instance_id: AWS Connect instance ID
        initial_contact_id: Initial contact ID
        attributes: Dictionary of attributes to set

    Returns:
        Response from AWS Connect
    """
    try:

        response = connect_client.update_contact_attributes(
            InstanceId=instance_id,
            InitialContactId=initial_contact_id,
            Attributes=attributes
        )

        print(f"✅ Connect attributes updated successfully")
        return response

    except Exception as e:
        print(f"❌ Error updating Connect attributes: {str(e)}")
        raise

def construct_sf_link(type, id):
    """
    Construct Salesforce link

    Args:
        type: Type of record (Case, Task, etc.)
        id: Salesforce record ID

    Returns:
        Salesforce link
    """
    sf_base_url = os.environ.get('salesforce_base_url_sandbox', 'https://qcellsnorthamerica123--qa.sandbox.lightning.force.com')
    return f"{sf_base_url}/lightning/r/{type}/{id}/view"

def find_id(type, number):
    """
    Find case ID by case number
    Uses shared authentication utilities

    Args:
        case_number: Case number to search

    Returns:
        dict: Contact info and cases
    """
    try:
        # Search for case by case number using shared sf_query
        if type == 'Case':
            query = f"""
            SELECT Id
            FROM Case
            WHERE CaseNumber = '{number}'
            LIMIT 1
            """
            print(f"Searching for case with case number: {number}")
        else:
            query = f"""
            SELECT Id
            FROM Task
            Where Task_Number__c = '{number}'
            LIMIT 10
            """
            print(f"Searching for task with task number: {number}")
        
        result = sf_query(query)
        print(result)
        data = result['data']

        if data['totalSize'] == 0:
            print(f"No case/ticket found for: {number}")
            return None

        resp = data['records'][0]
        id = resp['Id']

        return id

    except Exception as e:
        print(f"Error in find_contact_and_cases_by_phone: {str(e)}")
        raise

def create_or_get_contact(phone, caller_name, email, record_type_id):
    if not phone:
        return None

    # Normalize phone (basic)
    phone_clean = phone.replace(" ", "")

    # Try to find existing contact
    soql = "SELECT Id FROM Contact " f"WHERE Email = '{email}' " "LIMIT 1"

    res = sf_query(soql)
    records = res["data"].get("records", [])

    if records:
        return records[0]["Id"]

    # Create new contact if not found
    token_res = get_access_token()
    instance_url = token_res["instance_url"].rstrip("/")
    access_token = token_res["access_token"]

    create_url = f"{instance_url}/services/data/v59.0/sobjects/Contact/"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    contact_payload = {
        "LastName": caller_name or "Unknown Caller",
        "Phone": phone_clean,
        "Email": email
    }

    print(f"Contact payload: {str(contact_payload)}")
    resp = requests.post(create_url, headers=headers, json=contact_payload, timeout=15)
    print(resp.text)
    resp.raise_for_status()

    return resp.json().get("id")

def create_case(case_information, customer_details):
    
    token_res = get_access_token()
    instance_url = token_res["instance_url"].rstrip("/")
    access_token = token_res["access_token"]

    date_time = datetime.now()
    case_subject = f"{case_information.get('caseType')}-{customer_details.get('callerName')}-{case_information.get('callReason')}-date_time"

    record_type_id = None
    if customer_details.get('customerType') == 'Installer':
        record_type_id = os.environ.get("INSTALLER_TYPE_ID")
    elif customer_details.get('customerType') == 'Homeowner':
        record_type_id = os.environ.get("HOMEOWNER_TYPE_ID")
    

    if case_information.get('caseType') == 'Claim':
        case_payload = {
            'Subject': case_subject,
            'Description': case_information.get('descriptionOfIssue')
        }
        record_type_id = os.environ.get("SF_RECORD_TYPE_CLAIM")
        case_payload['RecordTypeId'] = record_type_id
        case_payload['Claimed_Qcells_Product__c'] = "Q.TRON AC" # This field is harcoded, we expect CSE/FAE to update it manually in salesforce
    elif case_information.get('caseType') == 'Inquiry':
        case_payload = {
            'Subject': case_subject,
            'Description': case_information.get('descriptionOfIssue')
        }
        lastName = customer_details.get('callerName').split(' ')[1]
        contact_id = create_or_get_contact(customer_details.get('phone'), lastName, customer_details.get('email'), record_type_id)
        case_payload["ContactId"] = contact_id
        record_type_id = os.environ.get("SF_RECORD_TYPE_INQUIRY")
        case_payload['RecordTypeId'] = record_type_id

    print(f"Case payload: {str(case_payload)}")

    # Create Case in Salesforce
    create_url = f"{instance_url}/services/data/v59.0/sobjects/Case/"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(create_url, headers=headers, json=case_payload)
        print(resp.text)

        if resp.status_code != 201:
            error_data = safe_json(resp.text)
            print(error_data)
            error_msg = error_data.get('message') if error_data else f"Failed to create task: {resp.status_code}"
            raise Exception(error_msg)

        resp_json = resp.json()
        case_id = resp_json.get("id")
        return case_id

        # return resp.json()

    except requests.exceptions.RequestException as e:
        raise Exception(f"Failed to create task: {str(e)}")


# Helper function to create HTTP responses
def response(status_code, body_content):
    """
    Create a standardized HTTP response for API Gateway

    Args:
        status_code: HTTP status code (200, 400, 404, 500, etc.)
        body_content: Dictionary to be returned as JSON body

    Returns:
        API Gateway compatible response object
    """
    return {
        'statusCode': status_code,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*'
        },
        'body': json.dumps(body_content)
    }


def lambda_handler(event, context):
    print(f"Received event: {json.dumps(event)}")

    try:
        # Extract taskId from queryStringParameters (for API Gateway GET requests)
        task_id_from_query = None
        instance_id_from_query = None
        contact_IdAWS = None
        if 'queryStringParameters' in event and event['queryStringParameters']:
            task_id_from_query = event['queryStringParameters'].get('taskId')
            print(f"TaskId from queryStringParameters: {task_id_from_query}")

            instance_id_from_query = event['queryStringParameters'].get('instanceId')
            print(f"InstanceId from queryStringParameters: {instance_id_from_query}")

            contact_IdAWS = event['queryStringParameters'].get('contact_IdAWS')
            print(f"InitialContactId from queryStringParameters: {contact_IdAWS}")

        # 1. Properly handle the API Gateway proxy wrapper
        if 'body' in event:
            # When coming from API Gateway, event['body'] is a string
            body = json.loads(event['body']) if isinstance(event['body'], str) else event['body']
        else:
            # Fallback for the Lambda Test Tab
            body = event

        # 2. IMPORTANT: Change all 'event.get' to 'body.get'
        action = body.get('action', 'fetch_tasks')
        task_number = body.get('taskNumber')
        # Use taskId from query string if not in body
        if task_id_from_query and not body.get('taskId'):
            body['taskId'] = task_id_from_query
            print(f"Using taskId from query string: {task_id_from_query}")
        
        if action == 'create_task_detail':
            task_detail_data = body.get('taskDetailData') # Changed from event.get
            if not task_detail_data:
                return response(400, {'success': False, 'error': 'taskDetailData is required'})

            result = create_task_detail(task_detail_data)
            
            task_detail_id = result.get('id', '') if isinstance(result, dict) else ''

            if not task_detail_id:
                raise Exception("Salesforce did not return a task ID")

            print(f"Task ID extracted: {task_detail_id}")

            return response(201, {'success': True, 'data': result})

        elif action == 'update_task':
            task_id = body.get('taskId') # Changed from event.get
            print(task_id)
            customer_details = body.get('customerDetails')
            case_information = body.get('caseInformation')
            call_status = body.get('callStatus')

            contactAttributes = get_contact_attributes(instance_id_from_query, contact_IdAWS)
            print(f"Contact attributes: {json.dumps(contactAttributes, indent=2)}")

            create_new_case = case_information.get('isNewCase')

            caseId = None
            if case_information.get('enggType') == 'CSE':
                if create_new_case:
                    caseId = create_case(case_information, customer_details)
                else:
                    caseId = find_id('Case', case_information.get('selectedCaseNumber'))
            else:
                if create_new_case:
                    caseId = create_case(case_information, customer_details)
                else:
                    caseId = find_id('Case', case_information.get('selectedCaseNumber'))
                    # TBD: Remove this logic, added because frontend send case id instead of case number.
                    if not caseId:
                        caseId = case_information.get('selectedCaseNumber')

            try:
                Call_Reason__c = case_information.get('callReason')
                Customer_Type_Value__c = customer_details.get('customerType')
                Caller_Name__c = customer_details.get('callerName')
                Mobile_Number__c = customer_details.get('phone')
                Customer_Email__c = customer_details.get('email')
                Main_Product__c = customer_details.get('mainProduct')
                Sub_Product__c = customer_details.get('subProduct')
                System_Config__c = customer_details.get('systemConfig')
                Stage__c = case_information.get('stage')
                Site_ID__c = customer_details.get('registrationSiteId')
                Status__c = case_information.get('status')
                Claim_Number__c = customer_details.get('claimNumber')
                Customer_CI__c = customer_details.get('customerInstaller')

            except Exception as e:
                print(f"Error extracting form data: {str(e)}")
                import traceback
                traceback.print_exc()

            date_time_text = datetime.utcnow().strftime("%m-%d-%Y-%H-%M-%S")
            random_number = random.randint(1000, 9999)
            taskSubject = f"Inquiry-{date_time_text}-{random_number}"

            Description_Of_Issue__c = case_information.get('descriptionOfIssue')
            Solution__c = case_information.get('solution')
            task_details = {
                'Name': taskSubject,
                'Description_Of_Issue__c': Description_Of_Issue__c,
                'Solution__c': Solution__c
            }

            result = create_task_detail(task_details)
            task_detail_id = result.get('id', '') if isinstance(result, dict) else ''

            if not task_detail_id:
                raise Exception("Salesforce did not return a task detail ID")

            description_text = []
            description_text.append("Files Uploaded:\n")
            sales_force_base_path = os.environ.get('sales_force_base_path', 'https://qcellsnorthamerica123--qa.sandbox.my.salesforce-setup.com/apex/S3Redirect?')
            bucket_name = None
            if 's3bucket' in contactAttributes:
                bucket_name = contactAttributes.get('s3bucket')
            if case_information.get('enggType') == 'CSE':
                dynamo_response = attachments_table.get_item(Key={'ContactAssociationId': contact_IdAWS})
                item = dynamo_response.get('Item', {})
                cseattachmentKeys = item.get('cseattachmentKeys', [])
                for attachment in cseattachmentKeys:
                    description_text.append(f"{sales_force_base_path}bucket={bucket_name}&fileKey={attachment}\n")
                '''
                if 'cseattachmentKeys' in contactAttributes:
                    attachments = contactAttributes['cseattachmentKeys']
                    attachment_list = attachments.split(',')
                    for attachment in attachment_list:
                        description_text.append(f"{sales_force_base_path}bucket={bucket_name}&fileKey={attachment}\n")
                    description_text.append(f"\n")
                '''
            elif case_information.get('enggType') == 'FAE':
                dynamo_response = attachments_table.get_item(Key={'ContactAssociationId': contact_IdAWS})
                item = dynamo_response.get('Item', {})
                faeattachmentKeys = item.get('faeattachmentKeys', [])
                for attachment in faeattachmentKeys:
                    description_text.append(f"{sales_force_base_path}bucket={bucket_name}&fileKey={attachment}\n")
                '''
                if 'faeattachmentKeys' in contactAttributes:  
                    attachments = contactAttributes['faeattachmentKeys']
                    attachment_list = attachments.split(',')
                    for attachment in attachment_list:
                        description_text.append(f"{sales_force_base_path}bucket={bucket_name}&fileKey={attachment}\n")
                    description_text.append(f"\n")
                '''
            description_text_block = "\n".join(description_text)

            # Build task data with Connect details
            task_data = {
                'Customer_Type_Value__c': Customer_Type_Value__c,
                'Caller_Name__c': Caller_Name__c,
                'Mobile_Number__c': Mobile_Number__c,
                'Customer_Email__c': Customer_Email__c,
                'Call_Reason__c': Call_Reason__c,
                'Status__c': Status__c,
                'Main_Product__c': Main_Product__c,
                'Sub_Product__c': Sub_Product__c,
                'System_Config__c': System_Config__c,
                'Stage__c': Stage__c,
                'Site_ID__c': Site_ID__c,
                'Task_Detail__c': task_detail_id,
                'Claim_Number__c': Claim_Number__c,
                'Customer_C_I__c': Customer_CI__c,
                'Description' : description_text_block,
            }
            if caseId:
                task_data['WhatId'] = caseId

            print(f"Task data to send to Salesforce:")
            print(json.dumps(task_data, indent=2, default=str))

            if not task_id or not task_data:
                return response(400, {'success': False, 'error': 'taskId and taskData are required'})

            # Update Salesforce Task (with Connect details already included)
            result = update_task(task_id, task_data)
            print(f"✅ Salesforce task updated: {task_id}")

            # If support type is CSE, update contact attributes
            if case_information.get('enggType') == 'CSE':
                related_tickets = case_information.get('relatedTickets', [])
                related_tickets_comma_separated = ''
                for ticket in related_tickets:
                    url = None
                    if ticket.startswith('T-'):
                        ticket_id = find_id('Task', ticket)
                        url = construct_sf_link('Task', ticket_id)
                    related_tickets_comma_separated = related_tickets_comma_separated + f"{ticket}:{url},"
                updated_attributes = {
                    **contactAttributes,
                    'cseTaskId': task_id,
                    'isNewCaseCSE': case_information.get('isNewCase'),
                    'relatedTickets': related_tickets_comma_separated,
                    'caller_name': customer_details.get('callerName'),
                    'customer_email': customer_details.get('email'),
                    'customer_phone_number': customer_details.get('phone'),
                    'task_number': task_number,
                    
                    'mainProduct': customer_details.get('mainProduct'),
                    'subProduct': customer_details.get('subProduct'),
                    'systemConfig': customer_details.get('systemConfig'),
                    'stage': case_information.get('stage'),
                    'registrationSiteId': customer_details.get('registrationSiteId'),
                    'callReason': case_information.get('callReason'),
                    'status': case_information.get('status'),
                    'claimNumber': customer_details.get('claimNumber'),
                    'customerInstaller': customer_details.get('customerInstaller')
                }
                if caseId:
                    updated_attributes['cseCaseId'] = caseId
            elif case_information.get('enggType') == 'FAE':
                updated_attributes = {
                    **contactAttributes,
                    'faeTaskId': task_id,
                    'isNewCaseCSE': case_information.get('isNewCase')
                }
                if caseId:
                    updated_attributes['faeCaseId'] = caseId

            updated_attributes = {k: str(v) for k, v in updated_attributes.items() if v and str(v).strip()}

            update_result = update_connect_contact_attributes(instance_id_from_query, contact_IdAWS, updated_attributes)

            # Check contact attributes after update
            contactAttributes = get_contact_attributes(instance_id_from_query, contact_IdAWS)
            print(f"Updated Contact attributes: {json.dumps(contactAttributes, indent=2)}")

            try:
                send_survey_resp =send_survey_sms(customer_details.get('phone'), customer_details.get('callerName'), 
                customer_details.get('email'), task_number, contact_IdAWS, instance_id_from_query)
                print(f"Survey SMS sent: {send_survey_resp}")
            except Exception as e:
                print(f"Error sending survey SMS: {str(e)}")
                return response(500, {'success': False, 'error': str(e)})

            return response(200, {'success': True, 'data': result})

        elif action == 'find_id':
            # Handle find_id action for JavaScript frontend
            record_type = body.get('type')  # 'Case' or 'Task'
            record_number = body.get('number')  # Case number or Task number

            if not record_type or not record_number:
                return response(400, {'success': False, 'error': 'type and number are required'})

            # Call the find_id function
            record_id = find_id(record_type, record_number)

            if record_id:
                print(f"Found {record_type} with ID: {record_id}")
                return response(200, {'success': True, 'data': record_id})
            else:
                print(f"No {record_type} found with number: {record_number}")
                return response(404, {'success': False, 'error': f'No {record_type} found with number {record_number}'})

        else:
            return response(200, {
                'success': True,
                'data': {
                    'reason': "No matching action found."
                }
            })

    except Exception as e:
        print(f"Error: {str(e)}")
        return response(500, {'success': False, 'error': str(e)})
