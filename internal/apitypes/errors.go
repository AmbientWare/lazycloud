package apitypes

import "errors"

// Error is also the error an owner returns to refuse a request: the API
// answers with its code and message.
func (e *Error) Error() string { return e.Message }

// CodeOf is the code of the first *Error in err's chain, or Internal when
// it has none.
func CodeOf(err error) ErrorCode {
	if e, ok := errors.AsType[*Error](err); ok {
		return e.Code
	}
	return Internal
}
