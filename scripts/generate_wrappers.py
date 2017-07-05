import os
import sys
from string import Template, ascii_lowercase
#from ..cwrap import cwrap
#from ..cwrap.plugins import StandaloneExtension, GenericNN, NullableArguments, AutoGPU

#BASE_PATH = os.path.realpath(os.path.join(__file__, '..', '..', '..'))
BASE_PATH = os.environ['TORCH_PATH']
WRAPPER_PATH = os.path.join(BASE_PATH, 'torch', 'csrc', 'nn')
THNN_UTILS_PATH = os.path.join(BASE_PATH, 'torch', '_thnn', 'utils.py')


def import_module(name, path):
	if sys.version_info >= (3, 5):
		import importlib.util
		spec = importlib.util.spec_from_file_location(name, path)
		module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(module)
		return module
	elif sys.version_info >= (3, 0):
		from importlib.machinery import SourceFileLoader
		return SourceFileLoader(name, path).load_module()
	else:
		import imp
		return imp.load_source(name, path)

thnn_utils = import_module('torch._thnn.utils', THNN_UTILS_PATH)

FUNCTION_TEMPLATE = Template("""\
[[
  name: $name
  return: void
  cname: $cname
  arguments:
""")

COMMON_TRANSFORMS = {
	'THIndex_t': 'i64',
	'THCIndex_t': 'usize',
	'THInteger_t': 'i32',
	'int': 'i32'
}
COMMON_CPU_TRANSFORMS = {
	'THNNState*': 'void*',
	'THIndexTensor*': 'THLongTensor*',
	'THIntegerTensor*': 'THIntTensor*',
}
COMMON_GPU_TRANSFORMS = {
	'THCState*': 'void*',
	'THCIndexTensor*': 'THCudaLongTensor*',
}

TYPE_TRANSFORMS = {
	'Trait': {
		'int': 'i32',
		'long': 'i64',
		'THTensor*': '&mut TensorKind',
		'real': 'f32',
		'accreal': 'f64',
		'double': 'f64',
		'THIndexTensor*': '&mut TensorKind',
		'THIntegerTensor*': '&mut TensorKind',
		'THGenerator*': '&mut THGenerator'
	},
	'Float': {
		'THTensor*': 'THFloatTensor*',
		'real': 'float',
		'accreal': 'double',
	},
	'Double': {
		'THTensor*': 'THDoubleTensor*',
		'real': 'double',
		'accreal': 'double',
	},
	'CudaHalf': {
		'THCTensor*': 'THCudaHalfTensor*',
		'real': 'half',
		'accreal': 'float',
	},
	'Cuda': {
		'THCTensor*': 'THCudaTensor*',
		'real': 'float',
		'accreal': 'float',
	},
	'CudaDouble': {
		'THCTensor*': 'THCudaDoubleTensor*',
		'real': 'double',
		'accreal': 'double',
	},
}

def should_wrap_function(name):
	if name.startswith('LookupTable_'):
		return False
	return (name.endswith('updateOutput') or
		name.endswith('updateGradInput') or
		name.endswith('accGradParameters') or
		name.endswith('backward'))

for t, transforms in TYPE_TRANSFORMS.items():
	transforms.update(COMMON_TRANSFORMS)

for t in ['Float', 'Double']:
	TYPE_TRANSFORMS[t].update(COMMON_CPU_TRANSFORMS)
for t in ['CudaHalf', 'Cuda', 'CudaDouble']:
	TYPE_TRANSFORMS[t].update(COMMON_GPU_TRANSFORMS)

def rstype(arg):
	return TYPE_TRANSFORMS['Trait'].get(arg.type, arg.type)

def wrap_function_decl(name, arguments):
	cname = name
	type = 'Trait'
	declaration = '\tfn ' + cname + '(&mut self'
	for arg in arguments[1:]:
		declaration += ', ' + arg.name + ': ' 
		nexttype = TYPE_TRANSFORMS[type].get(arg.type, arg.type)
		if not arg.is_optional:
			declaration += nexttype
		else:
			declaration += '&mut Option<TensorKind>'
	declaration += ')'
	return declaration

def arg_cast(name, argtype, type):
	usename = name
	if "Tensor" in argtype:
		usename += '.inner() as *mut {}'.format(TYPE_TRANSFORMS[type][argtype][:-1])
	return usename
def arg_cast_inner(name, argtype, type):
	usename = name
	if "Tensor" in argtype:
		usename += ' as *mut {}'.format(TYPE_TRANSFORMS[type][argtype][:-1])
	return usename

def unwrap_option(arg):
	out = "\t\tlet mut {} = if let &mut Some(ref t) = {}".format(arg.name, arg.name)
	out += " {t.inner()} else { ::std::ptr::null_mut()};\n"
	return out

def wrap_function_impl(type, name, arguments):
	impl = ''
	for arg in arguments[1:]:
		if arg.is_optional:
			impl += unwrap_option(arg)
	cname = 'THNN_' + type + name
	impl += '\t\tunsafe {\n'
	impl += '\t\t\t' + cname + '(self.state'
	for arg in arguments[1:]:
		if arg.is_optional:
			impl += ', {}'.format(arg_cast_inner(arg.name, arg.type, type))
		else:
			impl += ', {}'.format(arg_cast(arg.name, arg.type, type))
	impl += ');\n'

	impl += '\t\t}\n'
	return impl

def generate_wrappers():
	wrap_backend_decl()
	wrap_backend_impls()
	generate_function_classes()
#    wrap_cunn()
#    wrap_generic()

def wrap_backend_decl():
	wrapper = "// Autogenerated - do not change\n"
	wrapper += "#![allow(non_snake_case)]\n\n"
	wrapper += "use rutorch::*;\n"
	wrapper += "use tensor::{Tensor, TensorKind};\n\n"
	#wrapper = '#include <TH/TH.h>\n\n\n'
	wrapper += 'pub trait BackendIntf : Sync {\n\n'
	#wrapper += "\tfn get_state(&self) ->  *mut ::std::os::raw::c_void;\n"
	nn_functions = thnn_utils.parse_header(thnn_utils.THNN_H_PATH)
	nn_functions = filter(lambda fn: "unfolded" not in fn.name, nn_functions)
	nn_functions = filter(lambda fn: should_wrap_function(fn.name), nn_functions)

	for fn in nn_functions:
		wrapper += wrap_function_decl(fn.name, fn.arguments) + ";\n"
	wrapper += "\n}"
	with open('src/nn/backends/backend.rs', 'w') as f:
		f.write(wrapper)

self_dict = { 'is_optional': False, 'name': '&mut self', 'type': 'self' }

def wrap_backend_impl_type(type):
	wrapper = "// Autogenerated - do not change\n"
	wrapper += "#![allow(non_snake_case)]\n"
	wrapper += "#![allow(non_camel_case)]\n\n"
	wrapper += "use tensor::{Tensor, TensorKind};\n"
	wrapper += "use nn::backends::backend::*;\n"
	wrapper += "use rutorch::*;\n\n"
	wrapper += "#[derive(Clone)]\n"
	wrapper += "pub struct THNN_{}Backend ".format(type) + "{\n"
	wrapper += "\tstate: *mut ::std::os::raw::c_void,\n"
	wrapper += "}\n\n"
	wrapper += "unsafe impl Sync for THNN_{}Backend ".format(type) + "{}\n"
	wrapper += "pub static {}Backend : THNN_{}Backend = THNN_{}Backend ".format(type, type, type)
	wrapper += "{state : 0 as *mut ::std::os::raw::c_void };\n"

	wrapper += "impl BackendIntf for THNN_{}Backend ".format(type) + " {\n"
	#wrapper += "\tfn get_state(&self) ->  *mut ::std::os::raw::c_void {\n"
	#wrapper += "\t\tself.state"
	#wrapper += "\t}"
	nn_functions = thnn_utils.parse_header(thnn_utils.THNN_H_PATH)
	nn_functions = filter(lambda fn: "unfolded" not in fn.name, nn_functions)
	nn_functions = filter(lambda fn: should_wrap_function(fn.name), nn_functions)

	for fn in nn_functions:
		wrapper += wrap_function_decl(fn.name, fn.arguments) + " {\n"
		wrapper += wrap_function_impl(type, fn.name, fn.arguments)
		wrapper += "\t}\n"

	wrapper += "}\n"
	with open('src/nn/backends/thnn_{}.rs'.format(type.lower()), 'w') as f:
		f.write(wrapper)

def wrap_backend_impls():
	for t in ['Float', 'Double']:
		wrap_backend_impl_type(t)

def build_header():
	header = "// Autogenerated - do not change\n"
	header += "#![allow(non_snake_case)]\n"
	header += "#![allow(non_camel_case)]\n\n"
	header += "use autograd::{Function, FuncIntf, FuncDelegate, FIWrap};\n"
	header += "use tensor::{OptTensorKindList, TensorKindList, TensorKind, make_vec};\n"
	header += "use itertools::repeat_call;\n"
	header += "use nn::backends::backend::*;\n\n\n"
	return header

def build_forward(name, args):
	forward = "let backend = input_list[0].backend();\n"
	forward += "self.save_for_backward("
	return forward

def build_backward(name, args):
	backward = ""
	return backward


def build_args(name, args):
	fn_class = "#[builder(pattern=\"owned\")]\n"
	fn_class += "#[derive(Builder, Clone, Default)]\n"
	fn_class += "pub struct {}Args ".format(name) + "{\n"
	for arg in args:
		fn_class += "\tpub {}: {},\n".format(arg.name, rstype(arg))
	fn_class += "}\n"
	return fn_class

def _make_function_class_criterion(class_name, update_output, update_grad_input, acc_grad_parameters):
	weight_arg_idx = -1
	for i, arg in enumerate(update_output.arguments):
		if arg.name.startswith('weight'):
			weight_arg_idx = i
			break

	buffers_idx = []
	additional_arg_idx = 0
	for arg in update_output.arguments[4:]:
		if not arg.name.startswith('weight') and arg.type == 'THTensor*':
			buffers_idx.append(additional_arg_idx)
		additional_arg_idx += 1

	full_args = update_output.arguments[4:]
	additional_args = ["self.args.{}".format(arg.name) for arg in full_args if "Tensor" not in arg.type]

	weightstr = ""
	if weight_arg_idx >= 0:
		weightstr += "\t\tlet mut weight = if input_list.len() > 2 {Some(input_list[2].clone())} else { None };\n"
		idx = weight_arg_idx - 4
		additional_args.insert(idx, "&mut weight")
	bufferstr = "" 
	for i, idx in enumerate(buffers_idx):
		bufferstr += "\t\tself.saved_tensors.push(input.new(1));\n"
		additional_args.insert(idx, "&mut self.saved_tensors[{}]".format(i))

	def build_forward_class_criterion():
		forward = "\t\tlet mut backend = input_list[0].backend();\n"
		forward += "\t\tself.save_for_backward(input_list);\n"
		forward += "\t\tlet mut input = input_list[0].clone();\n"
		forward += weightstr
		forward += bufferstr
		forward += "\t\tlet mut output = input.new(1);\n"
		forward += "\t\tbackend.{}(&mut input, &mut input_list[1].clone(), &mut output, ".format(update_output.name)
		forward +=  ', '.join(arg for arg in additional_args) + ");\n"
		forward += "\t\tvec![output]"
		return forward

	def build_backward_class_criterion():
		backward = "\t\tlet mut input_list = self.saved_tensors();\n"
		backward += "\t\tlet (mut input, mut target) = (input_list[0].clone(), input_list[1].clone());\n"
		backward += weightstr
		backward += "\t\tlet mut grad_output = grad_output_list.remove(0).unwrap();\n"
		backward += "\t\tlet mut backend = input.backend();\n"
		backward += "\t\tlet mut grad_input = grad_output.new(input.size()).zero_().clone();\n"
		backward += "\t\tbackend.{}(&mut input, &mut target, &mut grad_input, ".format(update_grad_input.name)
		backward += ', '.join(arg for arg in additional_args) + ");\n"
		backward += "\t\tlet dims = make_vec(1, grad_input.dim() as usize);"
		backward += "\t\tlet mut grad_output_expanded = grad_output.view(dims.as_slice());\n"
		backward += "\t\tlet mut grad_output_expanded = grad_output_expanded.expand_as(&grad_input);\n"		
		backward += "\t\tgrad_input.mult_(&grad_output_expanded);\n"
		backward += "\t\tvec![Some(grad_input), None]"
		return backward

	fn_class = ""
	args = [arg for arg in full_args if "Tensor" not in arg.type]
	needs_args = len(args) >  0
	if needs_args:
		fn_class += build_args(class_name, args)
		fn_class += "impl_func_args!({}, {}Args);\n".format(class_name, class_name)
	else:
		fn_class += "impl_func!({});\n".format(class_name)


	fn_class += "impl FuncIntf for {} ".format(class_name) + " {\n"
	fn_class += "\tfn forward(&mut self, input_list: &mut TensorKindList) -> TensorKindList {\n"
	fn_class += build_forward_class_criterion()
	fn_class += "\n\t}\n"
	fn_class += "\tfn backward(&mut self, grad_output_list: &mut OptTensorKindList) -> OptTensorKindList {\n"
	fn_class += build_backward_class_criterion()
	fn_class += "\n\t}\n"
	fn_class += "}\n\n"
	return fn_class

def _find_buffers(args, ignored_args):
	additional_arg_idx = 0
	buffers = []
	for arg in args:
		if arg.name in ignored_args:
			continue
		if arg.type == 'THTensor*':
			buffers.append((additional_arg_idx, arg.name))
		additional_arg_idx += 1
	return buffers

def _make_function_class(class_name, update_output, update_grad_input, acc_grad_parameters):
	def has_argument(fn, name):
		for arg in fn.arguments:
			if arg.name == name:
				return True
		return False
	save_output = has_argument(update_grad_input, 'output')
	needs_input = has_argument(update_grad_input, 'input')

	param_args = {'weight', 'bias'}
	ignored_args = {'weight', 'bias', 'gradWeight', 'gradBias', 'output'}
	expected_params = [arg for arg in update_output.arguments[3:]
					   if arg.name in param_args]
	buffers = {}
	buffers['update_output'] = _find_buffers(update_output.arguments[3:],
											 ignored_args)
	buffers['update_grad_input'] = _find_buffers(
		update_grad_input.arguments[4:], ignored_args)
	if acc_grad_parameters is not None:
		buffers['acc_grad_parameters'] = _find_buffers(
			acc_grad_parameters.arguments[3:], ignored_args)

	full_args = update_output.arguments[3:]
	args = [arg for arg in full_args if "Tensor" not in arg.type]

	tensor_idxs = [(idx, arg.is_optional) for idx, arg in enumerate(full_args) if "Tensor" in arg.type]
	output_args = ["self.args.{}".format(arg.name) for arg in full_args if "Tensor" not in arg.type]
	#added_args = ["self.args.{}".format(arg.name) for arg in full_args if "Tensor" not in arg.type]
	added_args = full_args

	start = 4 if needs_input else 3
	grad_input_args = [arg for arg in update_grad_input.arguments[start:] if "Tensor" not in arg.type]
	input_args = ["self.args.{}".format(arg.name) for arg in grad_input_args if "Tensor" not in arg.type]
	if len(grad_input_args) > len(args):
		args = grad_input_args

	for i, (idx, opt) in enumerate(tensor_idxs):
		if opt:
			output_args.insert(idx, "&mut Some(input_list[{}].clone())".format(i+1))
		else:
			output_args.insert(idx, "&mut input_list[{}].clone()".format(i+1))

	ga_start = 5 if save_output else 4

	tensor_idxs_input = [(idx, arg.is_optional) for idx, arg in enumerate(update_grad_input.arguments[ga_start:]) if "Tensor" in arg.type]
	for i, (idx, opt) in enumerate(tensor_idxs_input):
		offset = i+1 
		if save_output:
			offset += 1
		if opt:
			input_args.insert(idx, "&mut Some(saved[{}].clone())".format(offset))
		else:
			input_args.insert(idx, "&mut saved[{}].clone()".format(offset))

	is_inplace = update_output.arguments[-1].name == 'inplace'
	needs_args = len(args) >  0 or len(grad_input_args) > 0

	skip_grad_output_unwrap = update_grad_input.arguments[2].is_optional
	skip_input_unwrap = update_grad_input.arguments[1].is_optional
	def initialize_buffers(fn_name):
		print(class_name)
		print(full_args)
		additional_args = added_args
		for idx, name in buffers[fn_name]:
			# TODO: some buffers are necessary only for update output and can be
			# freed right afterwards
			buffer = buffers[name]
			print(buffer)
			additional_args = additional_args[:idx] + [buffer] + additional_args[idx:]
		print(additional_args)
		return tuple(additional_args)

	def build_forward():
		forward = "\t\tlet mut backend = input_list[0].backend();\n"
		if is_inplace:
			forward += "\t\tlet mut output = if self.args.inplace {\n"
			forward += "\t\t\tself.mark_dirty(input_list);\n"
			forward += "\t\t\tinput_list[0].clone()\n"
			forward += "\t\t} else {\n"
			forward += "\t\t\tinput_list[0].new(())\n"
			forward += "\t\t};\n"
		else:
			forward += "\t\tlet mut output = input_list[0].new(());\n"

		if save_output:
			forward += "\t\t{\n"
			forward += "\t\t\tlet mut save_list = input_list.clone();\n"
			forward += "\t\t\tsave_list.push(output.clone());\n"
			forward += "\t\t\tself.save_for_backward(&mut save_list);\n"
			forward += "\t\t}\n"
		else:
			forward += "\t\tself.save_for_backward(input_list);\n"


		forward += "\t\tlet mut input = input_list.remove(0);\n"
		forward += "\t\tbackend.{}(&mut input, &mut output, ".format(update_output.name)
		forward +=  ', '.join(arg for arg in output_args) + ");\n"
		forward += "\t\tvec![output]\n"
		return forward

	def build_backward():
		input = "&mut input, " if needs_input else ""
		backward = "\t\tlet mut saved = self.saved_tensors();\n"
		# XXX acknowledge that this is incomplete
		backward += '\t\tpanic!("backward will not work properly until save_for is done correctly.");\n' 
		backward += "\t\tunimplemented!();\n"
		if save_output:
			backward += "\t\tlet (mut input, mut output) = (saved[0].clone(), saved[1].clone());\n"
		else:
			backward += "\t\tlet mut input = saved[0].clone();\n"
		if skip_grad_output_unwrap:
			backward += "\t\tlet mut grad_output = grad_output_list.remove(0);\n"
		else:
			backward += "\t\tlet mut grad_output = grad_output_list.remove(0).unwrap();\n"
		backward += "\t\tlet needs_input_grad = self.needs_input_grad().clone();\n"
		backward += "\t\tlet mut grad_input_result : Option<TensorKind> = None;\n"
		backward += "\t\tlet mut backend = input.backend();\n"

		# update_grad_input()
		#ext_args = initialize_buffers('update_grad_input')
		backward += "\t\tif needs_input_grad[0] {\n"
		backward += "\t\t\tlet mut grad_input = input.new(());\n"
		backward += "\t\t\tbackend.{}({}&mut grad_output, &mut grad_input".format(update_grad_input.name, input)
		if save_output:
			backward += ", &mut output"

		gi_args = input_args
		if len(input_args) > 0:
			backward +=  ', ' + ', '.join(arg for arg in gi_args) + ");\n"
		else:
			backward += ");\n"
		backward += "\t\t\tgrad_input_result = Some(grad_input);\n"
		backward += "\t\t}\n"

		# acc_grad_parameters()
		if acc_grad_parameters:
			backward += "\t\tif needs_input_grad[1..].iter().any(|t| *t) {\n"

			backward += "\t\t}\n"

		backward += "\t\tlet result = vec![grad_input_result];\n"
		backward += "\t\t//append additional arguments"
		backward += "\t\tresult"
		return backward
		backward += "\t\tlet mut grad_output = grad_output_list.remove(0).unwrap();\n"
		backward += "\t\tlet mut backend = input.backend();\n"
		backward += "\t\tlet mut grad_input = grad_output.new(()).resize_as_(&input).zero_().clone();\n"
		backward += "\t\tbackend.{}(&mut input, &mut target, &mut grad_input, ".format(update_grad_input.name)
		backward += ', '.join(arg for arg in input_args) + ");\n"
		backward += "\t\tlet mut grad_output_expanded = grad_output.view(make_vec(1, grad_input.dim() as usize).as_slice());\n"
		backward += "\t\tgrad_output_expanded = grad_output_expanded.expand_as(&grad_input);\n"
		backward += "\t\tgrad_input.mult_(&grad_output_expanded);\n"
		backward += "\t\tvec![Some(grad_input), None]"
		return backward

	fn_class = ""
	if needs_args:
		fn_class += build_args(class_name, args)
		fn_class += "impl_func_args!({}, {}Args);\n".format(class_name, class_name)
	else:
		fn_class += "impl_func!({});\n".format(class_name)

	fn_class += "impl FuncIntf for {} ".format(class_name) + " {\n"
	fn_class += "\tfn forward(&mut self, input_list: &mut TensorKindList) -> TensorKindList {\n"
	fn_class += build_forward()
	fn_class += "\n\t}\n"
	fn_class += "\tfn backward(&mut self, grad_output_list: &mut OptTensorKindList) -> OptTensorKindList {\n"
	fn_class += build_backward()
	fn_class += "\n\t}\n"
	fn_class += "}\n\n"
	return fn_class


def generate_function_classes():
	auto = build_header()

	nn_functions = thnn_utils.parse_header(thnn_utils.THNN_H_PATH)
	function_list = list(filter(lambda fn: "unfolded" not in fn.name, nn_functions))
	function_by_name = {fn.name: fn for fn in function_list}
	classes_to_generate = {fn.name.partition('_')[0] for fn in function_list}
	# make partition output deterministic
	#classes_to_generate = sorted(classes_to_generate.items(), key=lambda x: x.name)
	exceptions = {
		'Linear',
		'IndexLinear',
		'SpatialFullConvolution',
		'SpatialConvolutionMM',
		'SparseLinear',
		'TemporalConvolution',
		'SpatialAveragePooling',
		'SpatialMaxPooling',
		'SpatialDilatedMaxPooling',
		'SpatialMaxUnpooling',
		'SpatialAdaptiveMaxPooling',
		'SpatialAdaptiveAveragePooling',
		'VolumetricAveragePooling',
		'VolumetricMaxPooling',
		'VolumetricMaxUnpooling',
		'VolumetricConvolution',
		'VolumetricFullConvolution',
		'VolumetricConvolutionMM',
		'TemporalMaxPooling',
		'BatchNormalization',
		'LookupTable',
		'PReLU',
		'RReLU',
		'Threshold',
		'LeakyReLU',
		'GRUFused',
		'LSTMFused',
		'unfolded',
	}
	name_remap = {
		'TemporalConvolution': 'Conv1d',
		'SpatialDilatedConvolution': 'DilatedConv2d',
		'SpatialMaxUnpooling': 'MaxUnpool2d',
		'SpatialReflectionPadding': 'ReflectionPad2d',
		'SpatialReplicationPadding': 'ReplicationPad2d',
		'VolumetricReplicationPadding': 'ReplicationPad3d',
		'VolumetricMaxUnpooling': 'MaxUnpool3d',
		'SoftMax': 'Softmax',
		'LogSoftMax': 'LogSoftmax',
		'HardTanh': 'Hardtanh',
		'HardShrink': 'Hardshrink',
		'SoftPlus': 'Softplus',
		'SoftShrink': 'Softshrink',
		'MSECriterion': 'MSELoss',
		'AbsCriterion': 'L1Loss',
		'BCECriterion': '_BCELoss',  # TODO: move the glue code into THNN
		'ClassNLLCriterion': 'NLLLoss',
		'DistKLDivCriterion': 'KLDivLoss',
		'SpatialClassNLLCriterion': 'NLLLoss2d',
		'MultiLabelMarginCriterion': 'MultiLabelMarginLoss',
		'MultiMarginCriterion': 'MultiMarginLoss',
		'SmoothL1Criterion': 'SmoothL1Loss',
		'SoftMarginCriterion': 'SoftMarginLoss',
	}

	classes_to_generate -= exceptions
	# make end result deterministic
	classes_to_generate = sorted([fn for fn in classes_to_generate])
	for fn in classes_to_generate:
		update_output = function_by_name[fn + '_updateOutput']
		update_grad_input = function_by_name[fn + '_updateGradInput']
		acc_grad_parameters = function_by_name.get(fn + '_accGradParameters')
		class_name = name_remap.get(fn, fn)
		# This has to call a function to retain correct references to functions
		if 'Criterion' in fn:
			auto += _make_function_class_criterion(class_name, update_output,
												 update_grad_input, acc_grad_parameters)
		else:
			auto += _make_function_class(class_name, update_output,
									   update_grad_input, acc_grad_parameters)
	with open('src/nn/_functions/thnn/auto.rs', 'w') as f:
		f.write(auto)

def wrap_function(name, type, arguments):
	cname = 'THNN_' + type + name
	declaration = ''
	declaration += cname + \
		'(' + ', '.join(TYPE_TRANSFORMS[type].get(arg.type, arg.type) for arg in arguments) + ');\n'
	declaration += FUNCTION_TEMPLATE.substitute(name=type + name, cname=cname)
	indent = ' ' * 4
	dict_indent = ' ' * 6
	prefix = indent + '- '
	for arg in arguments:
		if not arg.is_optional:
			declaration += prefix + TYPE_TRANSFORMS[type].get(arg.type, arg.type) + ' ' + arg.name + '\n'
		else:
			t = TYPE_TRANSFORMS[type].get(arg.type, arg.type)
			declaration += prefix + 'type: ' + t + '\n' + \
				dict_indent + 'name: ' + arg.name + '\n' + \
				dict_indent + 'nullable: True' + '\n'
	declaration += ']]\n\n\n'
	return declaration

def wrap_nn():
	#wrapper = '#include <TH/TH.h>\n\n\n'
	wrapper = ''
	nn_functions = thnn_utils.parse_header(thnn_utils.THNN_H_PATH)
	for fn in nn_functions:
		wrapper += wrap_function_trait(fn.name, fn.arguments)
	for fn in nn_functions:
		for t in ['Float', 'Double']:
			wrapper += wrap_function(fn.name, t, fn.arguments)
	with open('work/THNN.cwrap', 'w') as f:
		f.write(wrapper)
#    cwrap('torch/csrc/nn/THNN.cwrap', plugins=[
#        StandaloneExtension('torch._thnn._THNN'),
#        NullableArguments(),
#    ])


def wrap_cunn():
	wrapper = '#include <TH/TH.h>\n'
	wrapper += '#include <THC/THC.h>\n\n\n'
	cunn_functions = thnn_utils.parse_header(thnn_utils.THCUNN_H_PATH)
	for fn in cunn_functions:
		for t in ['CudaHalf', 'Cuda', 'CudaDouble']:
			wrapper += wrap_function(fn.name, t, fn.arguments)
	with open('torch/csrc/nn/THCUNN.cwrap', 'w') as f:
		f.write(wrapper)
	cwrap('torch/csrc/nn/THCUNN.cwrap', plugins=[
		StandaloneExtension('torch._thnn._THCUNN'),
		NullableArguments(),
		AutoGPU(has_self=False),
	])

GENERIC_FUNCTION_TEMPLATE = Template("""\
[[
  name: $name
  return: void
  options:
""")

def wrap_generic_function(name, backends):
	declaration = ''
	declaration += GENERIC_FUNCTION_TEMPLATE.substitute(name=name)
	for backend in backends:
		declaration += '    - cname: ' + name + '\n'
		declaration += '      backend: ' + backend['name'] + '\n'
		declaration += '      arguments:\n'
		for arg in backend['arguments']:
			declaration += '       - arg: ' + arg.type + ' ' + arg.name + '\n'
			if arg.is_optional:
				declaration += '         optional: True\n'
	declaration += ']]\n\n\n'
	return declaration


def wrap_generic():
	from collections import OrderedDict
	defs = OrderedDict()

	def should_wrap_function(name):
		if name.startswith('LookupTable_'):
			return False
		return (name.endswith('updateOutput') or
				name.endswith('updateGradInput') or
				name.endswith('accGradParameters') or
				name.endswith('backward'))

	def add_functions(name, functions):
		for fn in functions:
			if not should_wrap_function(fn.name):
				continue
			if fn.name not in defs:
				defs[fn.name] = []
			defs[fn.name] += [{
				'name': name,
				'arguments': fn.arguments[1:],
			}]

	add_functions('nn', thnn_utils.parse_header(thnn_utils.THNN_H_PATH))
	add_functions('cunn', thnn_utils.parse_header(thnn_utils.THCUNN_H_PATH))

	wrapper = ''
	for name, backends in defs.items():
		wrapper += wrap_generic_function(name, backends)
#    with open('target/work/THNN_generic.cwrap', 'w') as f:
#        f.write(wrapper)

#    cwrap('torch/csrc/nn/THNN_generic.cwrap', plugins=[
#        GenericNN(header=True),
#    ], default_plugins=False, destination='torch/csrc/nn/THNN_generic.h')

#    cwrap('torch/csrc/nn/THNN_generic.cwrap', plugins=[
#        GenericNN(),
#    ], default_plugins=False)


if __name__ == '__main__':
	generate_wrappers()
