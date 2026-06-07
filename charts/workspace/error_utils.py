#!/usr/bin/env python3
"""
Error handling utilities for kube-coder.

Provides consistent error handling patterns, logging, and error response
generation across the codebase.
"""

import traceback
import sys
import json
from typing import Dict, Any, Optional, Callable, Type, Union
from functools import wraps

from http_utils import (
    HTTPError, BadRequestError, UnauthorizedError, ForbiddenError,
    NotFoundError, ValidationError, InternalServerError, BadGatewayError
)

# -------------------------------------------------------------------
# Error Logging
# -------------------------------------------------------------------

def log_error(
    error: Exception,
    context: Optional[Dict[str, Any]] = None,
    level: str = 'ERROR'
) -> None:
    """
    Log an error with consistent formatting.
    
    Args:
        error: Exception to log
        context: Additional context information
        level: Log level (ERROR, WARNING, INFO)
    """
    context_str = f" {json.dumps(context)}" if context else ""
    
    print(
        f"[{level}] {type(error).__name__}: {str(error)}{context_str}",
        file=sys.stderr
    )
    
    # Include traceback for non-HTTP errors
    if not isinstance(error, HTTPError):
        traceback.print_exc()

def log_and_raise(
    error: Exception,
    context: Optional[Dict[str, Any]] = None,
    level: str = 'ERROR'
) -> None:
    """
    Log an error and re-raise it.
    
    Args:
        error: Exception to log and raise
        context: Additional context information
        level: Log level
    """
    log_error(error, context, level)
    raise error

# -------------------------------------------------------------------
# Error Wrappers and Decorators
# -------------------------------------------------------------------

def handle_errors(
    default_message: str = "An unexpected error occurred",
    log_level: str = 'ERROR'
) -> Callable:
    """
    Decorator to handle errors in functions.
    
    Args:
        default_message: Default error message if exception isn't HTTPError
        log_level: Log level for non-HTTP errors
    
    Returns:
        Decorated function
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except HTTPError:
                # Re-raise HTTP errors (they should be handled by caller)
                raise
            except Exception as e:
                # Log and convert to InternalServerError
                log_error(e, {'function': func.__name__}, log_level)
                raise InternalServerError(default_message)
        return wrapper
    return decorator

def retry_on_error(
    max_attempts: int = 3,
    delay: float = 1.0,
    exponential_backoff: bool = True,
    retry_on: Optional[Union[Type[Exception], tuple]] = None
) -> Callable:
    """
    Decorator to retry a function on certain errors.
    
    Args:
        max_attempts: Maximum number of retry attempts
        delay: Base delay between attempts (seconds)
        exponential_backoff: Whether to use exponential backoff
        retry_on: Exception type(s) to retry on (default: all exceptions)
    
    Returns:
        Decorated function
    """
    if retry_on is None:
        retry_on = Exception
    
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_error = None
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except retry_on as e:
                    last_error = e
                    if attempt == max_attempts - 1:
                        break
                    
                    # Calculate delay with optional exponential backoff
                    current_delay = delay * (2 ** attempt) if exponential_backoff else delay
                    
                    log_error(
                        e,
                        {
                            'function': func.__name__,
                            'attempt': attempt + 1,
                            'max_attempts': max_attempts,
                            'retrying_in': current_delay
                        },
                        'WARNING'
                    )
                    
                    import time
                    time.sleep(current_delay)
            
            # All attempts failed
            raise last_error
        return wrapper
    return decorator

# -------------------------------------------------------------------
# Validation Utilities
# -------------------------------------------------------------------

def validate_required_fields(
    data: Dict[str, Any],
    required_fields: list,
    field_types: Optional[Dict[str, type]] = None
) -> None:
    """
    Validate that required fields are present and have correct types.
    
    Args:
        data: Data dictionary to validate
        required_fields: List of required field names
        field_types: Dictionary mapping field names to expected types
    
    Raises:
        ValidationError: If validation fails
    """
    errors = []
    
    # Check required fields
    for field in required_fields:
        if field not in data:
            errors.append(f"Missing required field: {field}")
    
    # Check field types if provided
    if field_types:
        for field, expected_type in field_types.items():
            if field in data and not isinstance(data[field], expected_type):
                errors.append(
                    f"Field '{field}' must be of type {expected_type.__name__}, "
                    f"got {type(data[field]).__name__}"
                )
    
    if errors:
        raise ValidationError("Validation failed", {'errors': errors})

def validate_string_field(
    field_name: str,
    value: Any,
    min_length: Optional[int] = None,
    max_length: Optional[int] = None,
    pattern: Optional[str] = None,
    allowed_values: Optional[list] = None
) -> None:
    """
    Validate a string field with various constraints.
    
    Args:
        field_name: Name of the field (for error messages)
        value: Value to validate
        min_length: Minimum length requirement
        max_length: Maximum length requirement
        pattern: Regex pattern to match
        allowed_values: List of allowed values
    
    Raises:
        ValidationError: If validation fails
    """
    errors = []
    
    if not isinstance(value, str):
        errors.append(f"Field '{field_name}' must be a string")
        raise ValidationError("Validation failed", {'errors': errors})
    
    if min_length is not None and len(value) < min_length:
        errors.append(f"Field '{field_name}' must be at least {min_length} characters")
    
    if max_length is not None and len(value) > max_length:
        errors.append(f"Field '{field_name}' must be at most {max_length} characters")
    
    if pattern is not None:
        import re
        if not re.match(pattern, value):
            errors.append(f"Field '{field_name}' does not match required pattern")
    
    if allowed_values is not None and value not in allowed_values:
        errors.append(f"Field '{field_name}' must be one of: {', '.join(allowed_values)}")
    
    if errors:
        raise ValidationError("Validation failed", {'errors': errors})

# -------------------------------------------------------------------
# Safe Execution Utilities
# -------------------------------------------------------------------

def safe_execute(
    func: Callable,
    *args,
    default_return: Any = None,
    log_errors: bool = True,
    **kwargs
) -> Any:
    """
    Execute a function safely, catching and logging any exceptions.
    
    Args:
        func: Function to execute
        args: Positional arguments
        default_return: Value to return if execution fails
        log_errors: Whether to log errors
        kwargs: Keyword arguments
    
    Returns:
        Function result or default_return on error
    """
    try:
        return func(*args, **kwargs)
    except Exception as e:
        if log_errors:
            log_error(e, {'function': func.__name__}, 'ERROR')
        return default_return

async def safe_execute_async(
    func: Callable,
    *args,
    default_return: Any = None,
    log_errors: bool = True,
    **kwargs
) -> Any:
    """
    Execute an async function safely, catching and logging any exceptions.
    
    Args:
        func: Async function to execute
        args: Positional arguments
        default_return: Value to return if execution fails
        log_errors: Whether to log errors
        kwargs: Keyword arguments
    
    Returns:
        Function result or default_return on error
    """
    try:
        return await func(*args, **kwargs)
    except Exception as e:
        if log_errors:
            log_error(e, {'function': func.__name__}, 'ERROR')
        return default_return

# -------------------------------------------------------------------
# Error Response Formatting
# -------------------------------------------------------------------

def format_error_response(
    error: Exception,
    include_traceback: bool = False
) -> Dict[str, Any]:
    """
    Format an exception as a standardized error response.
    
    Args:
        error: Exception to format
        include_traceback: Whether to include traceback in response
    
    Returns:
        Formatted error response dictionary
    """
    if isinstance(error, HTTPError):
        response = {
            'error': {
                'code': error.status_code,
                'message': error.message,
                'type': error.error_type,
                'details': error.details
            }
        }
    else:
        response = {
            'error': {
                'code': 500,
                'message': str(error),
                'type': 'internal_error',
                'details': {}
            }
        }
    
    if include_traceback:
        response['error']['traceback'] = traceback.format_exc()
    
    return response

def format_validation_errors(errors: list) -> Dict[str, Any]:
    """
    Format validation errors as a standardized error response.
    
    Args:
        errors: List of validation error messages
    
    Returns:
        Formatted error response
    """
    return {
        'error': {
            'code': 422,
            'message': 'Validation failed',
            'type': 'validation_error',
            'details': {'errors': errors}
        }
    }
