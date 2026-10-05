"""
GeoInbox - Background Task Management

Utilities for running tasks in background threads with progress reporting.
"""

from qgis.PyQt.QtCore import QThread, QObject, pyqtSignal
from qgis.core import QgsTask, QgsApplication

from typing import Optional, Callable, Any
from functools import wraps


class BackgroundWorker(QObject):
    """
    Generic background worker for running tasks in a separate thread.
    
    Signals:
        started: Emitted when task starts
        progress: Emitted with (current, total, message) for progress updates
        finished: Emitted with result when task completes successfully
        error: Emitted with (message, details) when task fails
        cancelled: Emitted when task is cancelled
    """
    
    started = pyqtSignal()
    progress = pyqtSignal(int, int, str)  # current, total, message
    finished = pyqtSignal(object)  # result
    error = pyqtSignal(str, str)  # message, details
    cancelled = pyqtSignal()
    
    def __init__(self, task_func: Callable, *args, **kwargs):
        """
        Initialize the worker.
        
        Args:
            task_func: Function to run in background
            *args: Arguments to pass to task_func
            **kwargs: Keyword arguments to pass to task_func
        """
        super().__init__()
        self._task_func = task_func
        self._args = args
        self._kwargs = kwargs
        self._cancelled = False
    
    def cancel(self):
        """Request cancellation of the task."""
        self._cancelled = True
    
    @property
    def is_cancelled(self) -> bool:
        """Check if cancellation was requested."""
        return self._cancelled
    
    def run(self):
        """Execute the task."""
        self.started.emit()
        
        try:
            # Inject progress callback and cancel check into kwargs
            self._kwargs['progress_callback'] = self._report_progress
            self._kwargs['is_cancelled'] = lambda: self._cancelled
            
            result = self._task_func(*self._args, **self._kwargs)
            
            if self._cancelled:
                self.cancelled.emit()
            else:
                self.finished.emit(result)
                
        except Exception as e:
            if self._cancelled:
                self.cancelled.emit()
            else:
                self.error.emit(str(e), '')
    
    def _report_progress(self, current: int, total: int, message: str = ''):
        """Report progress."""
        self.progress.emit(current, total, message)


class BackgroundTaskRunner:
    """
    Manages running background tasks with proper thread handling.
    
    Usage:
        runner = BackgroundTaskRunner()
        runner.run(my_function, arg1, arg2, 
                   on_finished=handle_result,
                   on_error=handle_error)
    """
    
    def __init__(self):
        """Initialize the task runner."""
        self._thread: Optional[QThread] = None
        self._worker: Optional[BackgroundWorker] = None
    
    def is_running(self) -> bool:
        """Check if a task is currently running."""
        return self._thread is not None and self._thread.isRunning()
    
    def run(
        self,
        task_func: Callable,
        *args,
        on_started: Optional[Callable] = None,
        on_progress: Optional[Callable[[int, int, str], None]] = None,
        on_finished: Optional[Callable[[Any], None]] = None,
        on_error: Optional[Callable[[str, str], None]] = None,
        on_cancelled: Optional[Callable] = None,
        **kwargs
    ):
        """
        Run a task in the background.
        
        Args:
            task_func: Function to run
            *args: Arguments for task_func
            on_started: Callback when task starts
            on_progress: Callback for progress (current, total, message)
            on_finished: Callback when task completes (result)
            on_error: Callback on error (message, details)
            on_cancelled: Callback when cancelled
            **kwargs: Keyword arguments for task_func
        """
        # Cancel any existing task
        self.cancel()
        
        # Create thread and worker
        self._thread = QThread()
        self._worker = BackgroundWorker(task_func, *args, **kwargs)
        self._worker.moveToThread(self._thread)
        
        # Connect signals
        self._thread.started.connect(self._worker.run)
        
        if on_started:
            self._worker.started.connect(on_started)
        if on_progress:
            self._worker.progress.connect(on_progress)
        if on_finished:
            self._worker.finished.connect(on_finished)
        if on_error:
            self._worker.error.connect(on_error)
        if on_cancelled:
            self._worker.cancelled.connect(on_cancelled)
        
        # Cleanup on finish
        self._worker.finished.connect(self._cleanup)
        self._worker.error.connect(self._cleanup)
        self._worker.cancelled.connect(self._cleanup)
        
        # Start
        self._thread.start()
    
    def cancel(self):
        """Cancel the current task."""
        if self._worker:
            self._worker.cancel()
        self._cleanup()
    
    def _cleanup(self, *args):
        """Cleanup thread resources."""
        if self._thread:
            self._thread.quit()
            self._thread.wait(1000)  # Wait up to 1 second
            self._thread = None
        self._worker = None


class QGISBackgroundTask(QgsTask):
    """
    QGIS-native background task using QgsTask.
    
    Integrates with QGIS task manager for proper progress display.
    """
    
    def __init__(
        self,
        description: str,
        task_func: Callable,
        *args,
        on_finished: Optional[Callable[[Any], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        **kwargs
    ):
        """
        Initialize the QGIS task.
        
        Args:
            description: Task description for QGIS task manager
            task_func: Function to run
            *args: Arguments for task_func
            on_finished: Callback when task completes
            on_error: Callback on error
            **kwargs: Keyword arguments for task_func
        """
        super().__init__(description, QgsTask.CanCancel)
        self._task_func = task_func
        self._args = args
        self._kwargs = kwargs
        self._on_finished = on_finished
        self._on_error = on_error
        self._result = None
        self._error_message = None
    
    def run(self) -> bool:
        """Execute the task (called by QGIS task manager)."""
        try:
            # Inject progress callback and cancel check
            self._kwargs['progress_callback'] = self._report_progress
            self._kwargs['is_cancelled'] = self.isCanceled
            
            self._result = self._task_func(*self._args, **self._kwargs)
            return True
            
        except Exception as e:
            self._error_message = str(e)
            return False
    
    def _report_progress(self, current: int, total: int, message: str = ''):
        """Report progress to QGIS task manager."""
        if total > 0:
            self.setProgress(current * 100 / total)
    
    def finished(self, result: bool):
        """Called when task finishes (in main thread)."""
        if result and self._on_finished:
            self._on_finished(self._result)
        elif not result and self._on_error:
            self._on_error(self._error_message or "Task failed")


def run_in_background(
    description: str,
    task_func: Callable,
    *args,
    on_finished: Optional[Callable[[Any], None]] = None,
    on_error: Optional[Callable[[str], None]] = None,
    use_qgis_task: bool = True,
    **kwargs
) -> Optional[QGISBackgroundTask]:
    """
    Convenience function to run a task in the background.
    
    Args:
        description: Task description
        task_func: Function to run
        *args: Arguments for task_func
        on_finished: Callback when task completes
        on_error: Callback on error
        use_qgis_task: Use QGIS task manager (recommended)
        **kwargs: Keyword arguments for task_func
        
    Returns:
        QGISBackgroundTask if use_qgis_task is True, else None
    """
    if use_qgis_task:
        task = QGISBackgroundTask(
            description,
            task_func,
            *args,
            on_finished=on_finished,
            on_error=on_error,
            **kwargs
        )
        QgsApplication.taskManager().addTask(task)
        return task
    else:
        runner = BackgroundTaskRunner()
        runner.run(
            task_func,
            *args,
            on_finished=on_finished,
            on_error=lambda msg, details: on_error(msg) if on_error else None,
            **kwargs
        )
        return None


def with_progress(func: Callable) -> Callable:
    """
    Decorator that adds progress_callback and is_cancelled parameters to a function.
    
    The decorated function should accept:
        - progress_callback(current, total, message): Report progress
        - is_cancelled(): Check if task should stop
        
    Usage:
        @with_progress
        def my_task(data, progress_callback=None, is_cancelled=None):
            for i, item in enumerate(data):
                if is_cancelled and is_cancelled():
                    return None
                process(item)
                if progress_callback:
                    progress_callback(i + 1, len(data), f"Processing {item}")
            return result
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        # Ensure callbacks exist (even if no-op)
        if 'progress_callback' not in kwargs:
            kwargs['progress_callback'] = lambda c, t, m='': None
        if 'is_cancelled' not in kwargs:
            kwargs['is_cancelled'] = lambda: False
        return func(*args, **kwargs)
    return wrapper
