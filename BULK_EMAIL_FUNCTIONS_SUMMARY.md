# Bulk Email Validation Functions in views.py

## Summary
Found **7 main functions** related to bulk email validation, file upload, and list processing in [Email_validate_app/views.py](Email_validate_app/views.py).

---

## 1. `service_validate_emails(request)` - FILE UPLOAD HANDLER
**Lines: 323-382**

```python
def service_validate_emails(request):
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    try:
        if not request.session.get('logged_in'):
            if is_ajax:
                return JsonResponse({"status": "error", "message": "Login required"}, status=401)
            messages.warning(request, "You need to log in to access this service.")
            return redirect(reverse('login'))

        if request.method == 'POST':
            file = request.FILES.get('file_')

            if not file:
                logging.warning("Upload attempted but no file found in request.FILES")
                if is_ajax:
                    return JsonResponse({"status": "error", "message": "No file uploaded. Please select a file."}, status=400)
                messages.error(request, "No file uploaded! Please upload a file to proceed.")
                return redirect(reverse("services"))

            try:
                sanitized_table_name, file_id, mess = create_job(file, request)

                if mess == "Job Created":
                    file_record = ListFiles.objects.filter(pk=file_id).first()
                    if file_record:
                        file_record.table_name = sanitized_table_name
                        file_record.save()
                        logging.info(f"Updated table_name for file_id {file_id}: {sanitized_table_name}")
                    else:
                        logging.error(f"No record found with file_id {file_id}")
                else:
                    if is_ajax:
                        return JsonResponse({"status": "error", "message": f"File upload failed: {mess}"}, status=400)
                    messages.error(request, f"File upload failed: {mess}")
                    return redirect(reverse("services"))

                if is_ajax:
                    return JsonResponse({"status": "ok"})
                return redirect(reverse("services"))

            except Exception as file_error:
                logging.error(f"An error occurred during file validation: {file_error}")
                if is_ajax:
                    return JsonResponse({"status": "error", "message": str(file_error)}, status=500)
                messages.error(request, f"An error occurred: {str(file_error)}")
                return redirect(reverse("services"))

        if is_ajax:
            return JsonResponse({"status": "error", "message": "POST required"}, status=405)
        messages.error(request, "Invalid request method.")
        return redirect(reverse("services"))

    except Exception as e:
        logging.error(f"An unexpected error occurred: {e}")
        if is_ajax:
            return JsonResponse({"status": "error", "message": str(e)}, status=500)
        messages.error(request, "An unexpected error occurred. Please try again later.")
        return redirect(reverse("services"))
```

**Purpose:** Handles CSV file upload for bulk email validation. Calls `create_job()` to process the file and creates a new ListFiles record.

---

## 2. `Analyze(request)` - FILE ANALYSIS HANDLER
**Lines: 517-574**

```python
def Analyze(request):
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    table = request.GET.get("table_name")
    if not table:
        if is_ajax:
            return JsonResponse({"status": "error", "message": "No table name provided."}, status=400)
        return HttpResponse("No table name provided.", status=400)

    list_file = ListFiles.objects.filter(table_name=table).first()
    if list_file:
        list_file.free_analyze = -1
        list_file.save()

    column = find_email_column(table)
    if not column:
        if is_ajax:
            return JsonResponse({"status": "error", "message": "No email column found."}, status=400)
        return HttpResponse("No email column found.", status=400)

    column_name = column[0]

    if column_name != "_Emails_":
        with connection.cursor() as cursor:
            cursor.execute(
                f"ALTER TABLE `{table}` RENAME COLUMN `{column_name}` TO `_Emails_`"
            )
        column_name = "_Emails_"

    thread = Thread(target=run_analysis_in_background, args=(table, column_name))
    thread.start()

    if is_ajax:
        return JsonResponse({"status": "ok"})
    return redirect(reverse("services"))
```

**Purpose:** Analyzes uploaded file for email validity percentage. Finds the email column and renames it to standardized name, then runs background analysis in a separate thread.

---

## 3. `manage_credits(selected_option, table_name, user_id, timezone)` - CREDIT MANAGEMENT
**Lines: 1240-1325**

```python
def manage_credits(selected_option, table_name, user_id, timezone):
    print(f"manage_credits ==> {selected_option}--{table_name}--{user_id}")

    if not table_name.isidentifier():
        logger.error(f"Invalid table name provided: {table_name}")
        return "Invalid table name or validation error"

    # Credits already deducted at validation start — just return rows
    try:
        file_entry = ListFiles.objects.get(table_name=table_name)
    except ListFiles.DoesNotExist:
        logger.error(f"ListFiles entry for {table_name} not found.")
        return "File entry not found in ListFiles."

    if file_entry.credite_status == "Credited":
        try:
            with connection.cursor() as cursor:
                if selected_option in ['valid', 'invalid']:
                    cursor.execute(f"SELECT * FROM `{table_name}` WHERE validation_results = %s", [selected_option.capitalize()])
                else:
                    cursor.execute(f"SELECT * FROM `{table_name}`")
                columns = [col[0] for col in cursor.description]
                rows = cursor.fetchall()
            return [dict(zip(columns, row)) for row in rows]
        except Exception as e:
            logger.error(f"Error fetching rows from {table_name}: {str(e)}")
            return f"Error fetching results: {str(e)}"

    # Not yet credited — check if user has enough credits (fallback for old jobs)
    try:
        with connection.cursor() as cursor:
            cursor.execute(f"""
                SELECT SUM(CASE WHEN validation_results = 'Valid' THEN 1 ELSE 0 END) +
                       SUM(CASE WHEN validation_results = 'Invalid' THEN 1 ELSE 0 END)
                FROM `{table_name}`
            """)
            result = cursor.fetchone()
            row_count = result[0] if result and result[0] is not None else 0
    except Exception as e:
        logger.error(f"Error executing SQL for table {table_name}: {str(e)}")
        return f"Invalid table name or validation error. Error: {str(e)}"

    current_credit = get_current_credit(user_id)
    if row_count > current_credit:
        return str(row_count)

    now_utc = datetime.utcnow().replace(tzinfo=pytz.UTC)
    UsedCredits.objects.create(user_id=user_id, used_credits=row_count, used_date=now_utc)
    used_sum = UsedCredits.objects.filter(user_id=user_id).aggregate(Sum("used_credits"))['used_credits__sum'] or 0
    credits_obj, _ = CurrentCredits.objects.get_or_create(user_id=user_id)
    credits_obj.used_credits = used_sum
    credits_obj.current_credits = max(0, (credits_obj.total_credits or 0) - used_sum)
    credits_obj.save()
    file_entry.credite_status = "Credited"
    file_entry.save()

    try:
        with connection.cursor() as cursor:
            if selected_option in ['valid', 'invalid']:
                cursor.execute(f"SELECT * FROM `{table_name}` WHERE validation_results = %s", [selected_option.capitalize()])
            else:
                cursor.execute(f"SELECT * FROM `{table_name}`")
            columns = [col[0] for col in cursor.description]
            rows = cursor.fetchall()
        return [dict(zip(columns, row)) for row in rows]
    except Exception as e:
        logger.error(f"Error fetching rows from {table_name}: {str(e)}")
        return f"Error fetching results: {str(e)}"
    else:
        logger.warning(f"Insufficient credits: {current_credit} available, {row_count} required.")
        return str(row_count)
```

**Purpose:** Manages credit deduction for bulk validation jobs. Checks if user has enough credits, deducts them, and returns validation results filtered by status (valid/invalid).

---

## 4. `calculate_price(credits)` - PRICING CALCULATION
**Lines: 1307-1325**

```python
plans = [
    (5000, 0.007, "Plan 1"),
    (50000, 0.004, "Plan 2"),
    (100000, 0.003, "Plan 3"),
    (500000, 0.002, "Plan 4"),
    (1000000, 0.0024, "Plan 5"),
    (2000000, 0.001, "Plan 6")
]

def calculate_price(credits):
    for threshold, rate, plan_name in plans:
        if credits <= threshold:
            price = credits * rate
            return True, (price, rate)
    return False, "Interested in Buying Over 2 Million Credits? Contact Us!"
```

**Purpose:** Calculates the price for a given number of credits based on tiered pricing plans.

---

## 5. `download_results(request)` - DOWNLOAD VALIDATION RESULTS
**Lines: 1330-1404**

```python
def download_results(request):
    if request.method == "POST":
        is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        sld_option = request.POST.get('result')
        tablename = request.POST.get('table_name')
        filename = request.POST.get('file_name')
        user_id = get_user_id(request)
        timezone = request.POST.get('timezone')

        results = manage_credits(sld_option, tablename, user_id, timezone)
        print(f"/ results == > {results}")

        if isinstance(results, str):
            if not results.isdigit():
                return JsonResponse({"status": "error", "message": results}, status=500)
            current_credits = get_current_credit(user_id)
            need_c = int(results) - current_credits
            if need_c:
                minimum_credits = 150
                if need_c < minimum_credits:
                    need_c += 150

            result = calculate_price(need_c)
            if not result[0]:
                return JsonResponse({"status": "error", "message": str(result[1])}, status=400)

            price, plan_value = result[1]
            try:
                user_data = UserTable.objects.get(id=user_id)
            except UserTable.DoesNotExist:
                return JsonResponse({"status": "error", "message": "User not found."}, status=404)

            receipt_id = generate_receipt_id("Asia/Kolkata")
            client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))
            try:
                payment = client.order.create(data={
                    "amount": int(price * 100),
                    "currency": "USD",
                    "receipt": receipt_id,
                })
            except razorpay.errors.BadRequestError as e:
                return JsonResponse({"status": "error", "message": "Invalid request to payment gateway."}, status=400)
            except razorpay.errors.RazorpayError as e:
                return JsonResponse({"status": "error", "message": "Payment gateway error."}, status=502)

            return JsonResponse({
                "status":    "need_credits",
                "key_id":    settings.RAZORPAY_KEY_ID,
                "order_id":  payment['id'],
                "amount":    payment['amount'],
                "currency":  payment.get('currency', 'USD'),
                "user_name": user_data.user_name,
                "user_email": user_data.user_email,
                "user_id":   user_data.id,
                "credit":    need_c,
                "plan":      f"{plan_value:.4f}",
                "flow":      "payg",
                "need":      need_c,
                "current":   current_credits,
            })

        elif isinstance(results, list):
            df = pd.DataFrame(results)
            df.drop(columns=['result_reasons'], errors='ignore', inplace=True) #just in case if this column exists
            with tempfile.NamedTemporaryFile(delete=False, suffix='.csv') as temp_file:
                df.to_csv(temp_file.name, index=False)
            return FileResponse(open(temp_file.name, 'rb'), as_attachment=True, filename=f"{tablename}_{sld_option}.csv")

        if is_ajax:
            return JsonResponse({"status": "error", "message": "Unexpected result. Please contact support."}, status=500)
        messages.error(request, "Unexpected result format received. Please contact support.")
        return redirect('service')
```

**Purpose:** Handles downloading validation results. Filters results by status (valid/invalid), manages credits, and generates CSV file for download. If insufficient credits, initiates payment flow via Razorpay.

---

## 6. `delete_query(request)` - DELETE BULK JOB
**Lines: 1406-1442**

```python
@require_POST
def delete_query(request):
    """
    Handle file deletion with confirmation.
    User must type 'delete' to confirm.
    """
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    try:
        user_id = get_user_id(request)
        if not user_id:
            if is_ajax:
                return JsonResponse({"status": "error", "message": "Not authenticated"}, status=401)
            messages.error(request, "User not authenticated.")
            return redirect('services')

        table_name = request.POST.get('table_name_')
        file_name = request.POST.get('file_name')

        if not table_name or not file_name:
            if is_ajax:
                return JsonResponse({"status": "error", "message": "Missing required parameters"}, status=400)
            messages.error(request, "Missing required parameters.")
            return redirect('services')

        # Check if file belongs to the logged-in user
        file_record = ListFiles.objects.filter(table_name=table_name, user_id=user_id).first()
        if not file_record:
            if is_ajax:
                return JsonResponse({"status": "error", "message": "File not found or no permission"}, status=404)
            messages.error(request, "File not found or you don't have permission to delete it.")
            return redirect('services')

        # Update the job status to mark it as deleted instead of removing the record
        file_record.job_status = "Deleted"
        file_record.save()

        if is_ajax:
            return JsonResponse({"status": "ok", "message": f"File '{file_name}' has been deleted."})
        messages.success(request, f"File '{file_name}' has been successfully Deleted.")

    except Exception as e:
        print(f"Error deleting file: {e}")
        if is_ajax:
            return JsonResponse({"status": "error", "message": str(e)}, status=500)
        messages.error(request, f"An error occurred while deleting the file: {str(e)}")

    return redirect('services')
```

**Purpose:** Soft-deletes a bulk validation job by marking the job_status as "Deleted" instead of removing the record. Validates user ownership before deletion.

---

## 7. `verify_emails(request)` - MAIN BULK VALIDATION INITIATOR
**Lines: 3154-3261**

```python
def verify_emails(request):
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    table = request.GET.get("table_name")
    print(f"[DEBUG] table_name: {table}")

    if not table:
        if is_ajax:
            return JsonResponse({"status": "error", "message": "Missing table_name"}, status=400)
        return HttpResponseBadRequest("Missing table_name parameter")

    try:
        file_id = int(table.split("_")[1])
    except (IndexError, ValueError):
        if is_ajax:
            return JsonResponse({"status": "error", "message": "Invalid table name format"}, status=400)
        return HttpResponseBadRequest("Invalid table name format")

    ListFiles.objects.filter(file_id=file_id).update(total_count=0)

    file_name = f"{table}.csv"
    file_path = os.path.join(UPLOAD_FOLDER, file_name)

    if not os.path.isfile(file_path):
        if is_ajax:
            return JsonResponse({"status": "error", "message": f"File {file_name} not found."}, status=404)
        messages.error(request, f"File {file_name} not found at {file_path}.")
        return redirect("services")

    email_column = find_emailcolumn_file(file_path)
    if not email_column:
        ListFiles.objects.filter(table_name=table).update(job_status="Stopped")
        if is_ajax:
            return JsonResponse({"status": "error", "message": "No email column found."}, status=400)
        messages.error(request, "No email column found.")
        return redirect("services")

    # Count rows to check credits before validation starts
    try:
        with open(file_path, newline="", encoding="utf-8-sig") as _f:
            total_rows = sum(1 for _ in _f) - 1  # subtract header
    except Exception:
        total_rows = 0

    uid = _get_uid(request)
    current_credits = get_current_credit(uid)

    if total_rows > current_credits:
        need_c = total_rows - current_credits
        if need_c < 150:
            need_c += 150
        result = calculate_price(need_c)
        if not result[0]:
            if is_ajax:
                return JsonResponse({"status": "error", "message": str(result[1])}, status=400)
            messages.error(request, str(result[1]))
            return redirect("services")
        price, plan_value = result[1]
        try:
            user_data = UserTable.objects.get(id=uid)
        except UserTable.DoesNotExist:
            if is_ajax:
                return JsonResponse({"status": "error", "message": "User not found."}, status=404)
            return redirect("services")
        receipt_id = generate_receipt_id("Asia/Kolkata")
        client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))
        payment = client.order.create(data={"amount": int(price * 100), "currency": "USD", "receipt": receipt_id})
        return JsonResponse({
            "status":     "need_credits",
            "key_id":     settings.RAZORPAY_KEY_ID,
            "order_id":   payment['id'],
            "amount":     payment['amount'],
            "currency":   payment.get('currency', 'USD'),
            "user_name":  user_data.user_name,
            "user_email": user_data.user_email,
            "user_id":    user_data.id,
            "credit":     need_c,
            "plan":       f"{plan_value:.4f}",
            "flow":       "payg",
            "need":       need_c,
            "current":    current_credits,
        })

    # Enough credits — deduct before validation starts
    from django.utils.timezone import now as tz_now
    UsedCredits.objects.create(user_id=uid, used_credits=total_rows, used_date=tz_now())
    used_sum = UsedCredits.objects.filter(user_id=uid).aggregate(Sum("used_credits"))['used_credits__sum'] or 0
    credits_obj, _ = CurrentCredits.objects.get_or_create(user_id=uid)
    credits_obj.used_credits = used_sum
    credits_obj.current_credits = max(0, (credits_obj.total_credits or 0) - used_sum)
    credits_obj.save()
    ListFiles.objects.filter(table_name=table).update(credite_status="Credited")

    # Fetch user details for completion notification
    notify_email = ""
    notify_name = ""
    try:
        if uid:
            u = UserTable.objects.get(id=uid)
            notify_email = u.user_email or ""
            notify_name = u.user_name or ""
    except Exception:
        pass

    # Dispatch to Celery
    validate_email_list_task.delay(table, file_path, email_column, notify_email, notify_name)

    ListFiles.objects.filter(table_name=table).update(job_status="Processing")
    if is_ajax:
        return JsonResponse({"status": "ok"})
    return redirect("services")
```

**Purpose:** Main entry point for bulk email validation. Validates file exists, counts rows, checks user credits, deducts credits if sufficient, and dispatches the validation task to Celery for background processing.

---

## Related Supporting Functions:

### `insert_credits(request, user_id, credit)` - Lines 560-589
Inserts credits into user's account when they purchase credits.

### `insert_ip_credits(request, user_id, ip_credit)` - Lines 592-615
Inserts IP monitoring credits for IP blacklist monitoring feature.

---

## Database Models Used:
- `ListFiles` - Stores bulk validation job information
- `CurrentCredits` - Tracks user's current credit balance
- `UsedCredits` - Tracks credit usage history
- `UserTable` - User account information
- `EmailValidationLog` - Log of email validation activities

---

## Related URL Patterns:
These functions should be mapped to URL patterns in `urls.py` like:
- `/service-validate-emails/` → `service_validate_emails`
- `/analyze/` → `Analyze`
- `/download-results/` → `download_results`
- `/delete-query/` → `delete_query`
- `/verify-emails/` → `verify_emails`

---

**End of Bulk Email Functions Report**
