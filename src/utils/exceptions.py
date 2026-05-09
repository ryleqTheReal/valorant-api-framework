"""Contains all custom exceptions"""

CRITICAL_APP_ERROR = "CRITICAL_APP_ERROR"
STRUCTURE_VALIDATION_ERROR = "STRUCTURE_VALIDATION_ERROR"
ENDPOINT_STRUCTURE_VALIDATION_ERROR = "ENDPOINT_STRUCTURE_VALIDATION_ERROR"
FILE_RESOLUTION_ERROR = "FILE_RESOLUTION_ERROR"
UNKNOWN_OS_ERROR = "UNKNOWN_OS_ERROR"
RIOT_AUTHENTICATION_ERROR = "RIOT_AUTHENTICATION_ERROR"
REGION_NOT_FOUND_ERROR = "REGION_NOT_FOUND_ERROR"
VERSION_NOT_FOUND_ERROR = "VERSION_NOT_FOUND_ERROR"
FALLBACK_API_ERROR = "FALLBACK_API_ERROR"

class AppError(Exception):
    """Highest exception in hierarchy from which all other exceptions stem     

    Args:
        Exception (_type_): Thrown when everything went wrong
    """
    def __init__(self, *args: object) -> None:
        super().__init__(*args)
        self.is_critical: bool = True
        self.message: str = "Something went horribly wrong and the app crashed"
        self.internal_status: str = CRITICAL_APP_ERROR
        
    
# ------------ Structure Verification Errors ------------

class StructureValidationError(AppError):
    """Generic error wrapper for anything related to data structures"""
    def __init__(self, *args: object, message: str = "Something went wrong verifying the data structure") -> None:
        super().__init__(*args)
        self.is_critical: bool = False
        self.message: str = message
        self.internal_status: str = STRUCTURE_VALIDATION_ERROR

class EndpointValidationError(StructureValidationError):
    """Error for unexpected endpoint formatting"""
    def __init__(self, *args: object, message: str = "The endpoint is not structured correctly") -> None:
        super().__init__(*args)
        self.is_critical: bool = False
        self.message: str = message
        self.internal_status: str = ENDPOINT_STRUCTURE_VALIDATION_ERROR
        
# ------------ File Resolution Errors ------------

class PathResolutionError(AppError):
    """Generic error wrapper for anything related to filepaths"""
    def __init__(self, *args: object, message: str = "Something went wrong when resolving the path to the file") -> None:
        super().__init__(*args)
        self.is_critical: bool = False
        self.message: str = message
        self.internal_status: str = FILE_RESOLUTION_ERROR
        
class UnknownPlatformError(AppError):
    """The app is ran on an unknown platform and files cannot be resolved correctly"""
    def __init__(self, *args: object, message: str = "The app is ran on an unknown OS with unknown file locations. If this error happens on Windows/Mac reach out.") -> None:
        super().__init__(*args)
        self.is_critical: bool = True
        self.message: str = message
        self.internal_status: str = UNKNOWN_OS_ERROR
        
# ------------ Riot Auth Errors ------------

class AuthenticationError(AppError):
    """Generic error wrapper for anything related to riot authentication"""
    def __init__(self, *args: object, message: str = "Something went wrong when trying to create authenticated riot session") -> None:
        super().__init__(*args)
        self.is_critical: bool = True
        self.message: str = message
        self.internal_status: str = RIOT_AUTHENTICATION_ERROR
        
class RegionNotFoundError(AuthenticationError):
    """Error happens when the region could not be found"""
    def __init__(self, *args: object, message: str = "Could not find user's region") -> None:
        super().__init__(*args)
        self.is_critical: bool = False
        self.message: str = message
        self.internal_status: str = REGION_NOT_FOUND_ERROR

class VersionNotFoundError(AuthenticationError):
    """The Riot client version could not be found"""
    def __init__(self, *args: object, message: str = "Could not find the Riot client version") -> None:
        super().__init__(*args)
        self.is_critical: bool = True
        self.message: str = message
        self.internal_status: str = VERSION_NOT_FOUND_ERROR

class FallbackApiError(AuthenticationError):
    """The fallback API request to valorant-api.com failed"""
    def __init__(self, *args: object, message: str = "Fallback API request failed") -> None:
        super().__init__(*args)
        self.is_critical: bool = True
        self.message: str = message
        self.internal_status: str = FALLBACK_API_ERROR
        
