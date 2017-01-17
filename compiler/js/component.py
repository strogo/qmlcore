from compiler.js import get_package, split_name, escape
from compiler.js.code import process, parse_deps, generate_accessors, replace_enums, mangle_path
from compiler import lang
import json

def path_or_parent(path, parent):
	return '.'.join(mangle_path(path.split('.'))) if path else parent

class component_generator(object):
	def __init__(self, ns, name, component, prototype = False):
		self.ns = ns
		self.name = name
		self.component = component
		self.aliases = {}
		self.declared_properties = {}
		self.properties = []
		self.enums = {}
		self.assignments = {}
		self.animations = {}
		self.package = get_package(name)
		self.base_type = None
		self.children = []
		self.methods = {}
		self.signal_handlers = {}
		self.changed_handlers = {}
		self.key_handlers = {}
		self.signals = set()
		self.elements = []
		self.id = None
		self.prototype = prototype
		self.ctor = ''

		for child in component.children:
			self.add_child(child)

	def collect_id(self, id_set):
		if self.id is not None:
			id_set.add(self.id)
		for g in self.assignments.itervalues():
			if type(g) is component_generator and g.id:
				g.collect_id(id_set)
		for g in self.animations.itervalues():
			if type(g) is component_generator and g.id:
				g.collect_id(id_set)
		for g in self.children:
			g.collect_id(id_set)

	def assign(self, target, value):
		t = type(value)
		if t is lang.Component:
			value = component_generator(self.ns, self.package + ".<anonymous>", value)
		if t is str: #and value[0] == '"' and value[-1] == '"':
			value = value.replace("\\\n", "")
		self.assignments[target] = value

	def has_property(self, name):
		return (name in self.declared_properties) or (name in self.aliases) or (name in self.enums)

	def add_child(self, child):
		t = type(child)
		if t is lang.Property:
			self.properties.append(child)
			for name, default_value in child.properties:
				if self.has_property(name):
					raise Exception("duplicate property " + name)
				#print name, default_value, lang.value_is_trivial(default_value)
				self.declared_properties[name] = child
				if default_value is not None:
					if not lang.value_is_trivial(default_value):
						self.assign(name, default_value)
		elif t is lang.AliasProperty:
			if self.has_property(child.name):
				raise Exception("duplicate property " + child.name)
			self.aliases[child.name] = child.target
		elif t is lang.EnumProperty:
			if self.has_property(child.name):
				raise Exception("duplicate property " + child.name)
			self.enums[child.name] = child
		elif t is lang.Assignment:
			if child.target == 'id':
				raise Exception('assigning non-id for id')
			self.assign(child.target, child.value)
		elif t is lang.IdAssignment:
			self.id = child.name
			self.assign("id", child.name)
		elif t is lang.Component:
			self.children.append(component_generator(self.ns, self.package + ".<anonymous>", child))
		elif t is lang.Behavior:
			for target in child.target:
				if target in self.animations:
					raise Exception("duplicate animation on property " + target);
				self.animations[target] = component_generator(self.ns, self.package + ".<anonymous-animation>", child.animation)
		elif t is lang.Method:
			fullname, args, code = split_name(child.name), child.args, child.code
			path, name = fullname
			if child.event and len(name) > 2 and name != "onChanged" and name.startswith("on") and name[2].isupper(): #onXyzzy
				name = name[2].lower() + name[3:]
				fullname = path, name
				if name.endswith("Pressed"):
					name = name[0].upper() + name[1:-7]
					fullname = path, name
					if fullname in self.key_handlers:
						raise Exception("duplicate key handler " + child.name)
					self.key_handlers[fullname] = code
				elif name.endswith("Changed"):
					name = name[:-7]
					fullname = path, name
					if fullname in self.changed_handlers:
						raise Exception("duplicate signal handler " + child.name)
					self.changed_handlers[fullname] = code
				else:
					if fullname in self.signal_handlers:
						raise Exception("duplicate signal handler " + child.name)
					self.signal_handlers[fullname] = args, code
			else:
				if fullname in self.methods:
					raise Exception("duplicate method " + name)
				self.methods[fullname] = args, code
		elif t is lang.Constructor:
			self.ctor = "\t//custom constructor:\n\t" + child.code + "\n"
		elif t is lang.Signal:
			name = child.name
			if name in self.signals:
				raise Exception("duplicate signal " + name)
			self.signals.add(name)
		elif t is lang.ListElement:
			self.elements.append(child.data)
		elif t is lang.AssignmentScope:
			for assign in child.values:
				self.assign(child.target + '.' + assign.target, assign.value)
		else:
			raise Exception("unhandled element: %s" %child)

	def call_create(self, registry, ident_n, target, value, closure):
		assert isinstance(value, component_generator)
		ident = '\t' * ident_n
		code = '\n//creating component %s\n' %value.component.name
		code += '%s%s.__create(%s.__closure_%s = { })\n' %(ident, target, closure, target)
		if not value.prototype:
			c = value.generate_creators(registry, target, closure, ident_n)
			code += c
		return code

	def call_setup(self, registry, ident_n, target, value, closure):
		assert isinstance(value, component_generator)
		ident = '\t' * ident_n
		code = '\n//setting up component %s\n' %value.component.name
		code += '%svar %s = %s.%s\n' %(ident, target, closure, target)
		code += '%s%s.__setup(%s.__closure_%s)\n' %(ident, target, closure, target)
		code += '%sdelete %s.__closure_%s\n' %(ident, closure, target)
		if not value.prototype:
			code += '\n' + value.generate_setup_code(registry, target, closure, ident_n)
		return code

	def generate_ctor(self, registry):
		return "\t_globals.%s.apply(this, arguments);\n" %(registry.find_component(self.package, self.component.name)) + self.ctor

	def get_base_type(self, registry):
		return registry.find_component(self.package, self.component.name)

	def generate(self, registry):
		base_type = self.get_base_type(registry)
		ctor  = "/**\n * @constructor\n"
		ctor += " * @extends {_globals.%s}\n" %base_type
		ctor += " */\n"
		ctor += "\t_globals.%s = function(parent, _delegate) {\n%s\n}\n" %(self.name, self.generate_ctor(registry))
		return ctor

	def generate_animations(self, registry, parent):
		r = []
		for name, animation in self.animations.iteritems():
			var = "behavior_%s_on_%s" %(escape(parent), escape(name))
			r.append("\tvar %s = new _globals.%s(%s)" %(var, registry.find_component(self.package, animation.component.name), parent))
			r.append("\tvar %s__closure = { %s: %s }" %(var, var, var))
			r.append(self.call_create(registry, 1, var, animation, var + '__closure'))
			r.append(self.call_setup(registry, 1, var, animation, var + '__closure'))
			target_parent, target = split_name(name)
			if not target_parent:
				target_parent = parent
			else:
				target_parent = self.get_rvalue(parent, target_parent)
			r.append("\t%s.setAnimation('%s', %s);\n" %(target_parent, target, var))
		return "\n".join(r)

	def generate_prototype(self, registry, ident_n = 1):
		assert self.prototype == True

		#HACK HACK: make immutable
		registry.id_set = set(['context'])
		self.collect_id(registry.id_set)

		r = []
		ident = "\t" * ident_n

		r.append("%s_globals.%s.prototype.componentName = '%s'" %(ident, self.name, self.name))

		for name in self.signals:
			r.append("%s_globals.%s.prototype.%s = _globals.core.createSignal('%s')" %(ident, self.name, name, name))

		for _name, argscode in self.methods.iteritems():
			path, name = _name
			if path:
				raise Exception('no <id> qualifiers (%s) allowed in prototypes %s (%s)' %(path, name, self.name))
			args, code = argscode
			code = process(code, self, registry)
			r.append("%s_globals.%s.prototype.%s = function(%s) %s" %(ident, self.name, name, ",".join(args), code))

		for prop in self.properties:
			for name, default_value in prop.properties:
				args = ["_globals.%s.prototype" %self.name, "'%s'" %prop.type, "'%s'" %name]
				if lang.value_is_trivial(default_value):
					args.append(default_value)
				r.append("%score.addProperty(%s)" %(ident, ", ".join(args)))

		for name, prop in self.enums.iteritems():
			values = prop.values

			for i in xrange(0, len(values)):
				r.append("/** @const @type {number} */")
				r.append("%s_globals.%s.prototype.%s = %d" %(ident, self.name, values[i], i))
				r.append("/** @const @type {number} */")
				r.append("%s_globals.%s.%s = %d" %(ident, self.name, values[i], i))

			args = ["_globals.%s.prototype" %self.name, "'enum'", "'%s'" %name]
			if prop.default is not None:
				args.append("_globals.%s.%s" %(self.name, prop.default))
			r.append("%score.addProperty(%s)" %(ident, ", ".join(args)))

		base_type = self.get_base_type(registry)

		for _name, code in self.changed_handlers.iteritems():
			path, name = _name
			if path or not self.prototype: #sync with condition below
				continue

			assert not path
			code = process(code, self, registry)
			r.append("%s_globals.core._protoOnChanged(_globals.%s.prototype, '%s', (function(value) %s ))" %(ident, self.name, name, code))

		for _name, argscode in self.signal_handlers.iteritems():
			path, name = _name
			if path or not self.prototype or name == 'completed': #sync with condition below
				continue
			args, code = argscode
			code = process(code, self, registry)
			r.append("%s_globals.core._protoOn(_globals.%s.prototype, '%s', (function(%s) %s ))" %(ident, self.name, name, ", ".join(args), code))

		for _name, code in self.key_handlers.iteritems():
			path, name = _name
			if path or not self.prototype: #sync with condition below
				continue
			code = process(code, self, registry)
			r.append("%s_globals.core._protoOnKey(_globals.%s.prototype, '%s', (function(key, event) %s ))" %(ident, self.name, name, code))


		generate = False

		code = self.generate_creators(registry, 'this', '__closure', ident_n + 1).strip()
		if code:
			generate = True
		b = '\t%s_globals.%s.prototype.__create.call(this, __closure.__base = { })' %(ident, base_type)
		code = '%s_globals.%s.prototype.__create = function(__closure) {\n%s\n%s\n%s}' \
			%(ident, self.name, b, code, ident)

		setup_code = self.generate_setup_code(registry, 'this', '__closure', ident_n + 2).strip()
		b = '%s_globals.%s.prototype.__setup.call(this, __closure.__base); delete __closure.__base' %(ident, base_type)
		if setup_code:
			generate = True
		setup_code = '%s_globals.%s.prototype.__setup = function(__closure) {\n%s\n%s\n}' \
			%(ident, self.name, b, setup_code)

		if generate:
			r.append('')
			r.append(code)
			r.append(setup_code)
			r.append('')

		return "\n".join(r)

	def find_property(self, registry, property):
		if property in self.declared_properties:
			return self.declared_properties[property]
		if property in self.enums:
			return self.enums[property]
		if property in self.aliases:
			return self.aliases[property]

		base = registry.find_component(self.package, self.component.name)
		if base != 'core.CoreObject':
			return registry.components[base].find_property(registry, property)

	def check_target_property(self, registry, target):
		path = target.split('.')

		if len(path) > 1:
			if (path[0] in registry.id_set):
				return

			if not self.find_property(registry, path[0]):
				raise Exception('unknown property %s in %s (%s)' %(path[0], self.name, self.component.name))
		else: #len(path) == 1
			if not self.find_property(registry, target):
				raise Exception('unknown property %s in %s (%s)' %(target, self.name, self.component.name))

	def generate_creators(self, registry, parent, closure, ident_n = 1):
		r = []
		ident = "\t" * ident_n

		if not self.prototype:
			for name in self.signals:
				r.append("%s%s.%s = _globals.core.createSignal('%s').bind(%s)" %(ident, parent, name, name, parent))

			for prop in self.properties:
				for name, default_value in prop.properties:
					args = [parent, "'%s'" %prop.type, "'%s'" %name]
					if lang.value_is_trivial(default_value):
						args.append(default_value)
					r.append("\tcore.addProperty(%s)" %(", ".join(args)))

			for name, prop in self.enums.iteritems():
				raise Exception('adding enums in runtime is unsupported, consider putting this property (%s) in prototype' %name)

		for idx, gen in enumerate(self.children):
			var = "%s$child%d" %(escape(parent), idx)
			component = registry.find_component(self.package, gen.component.name)
			r.append("%svar %s = new _globals.%s(%s)" %(ident, var, component, parent))
			r.append("%s%s.%s = %s" %(ident, closure, var, var))
			code = self.call_create(registry, ident_n, var, gen, closure)
			r.append(code)

		for target, value in self.assignments.iteritems():
			if target == "id":
				if "." in value:
					raise Exception("expected identifier, not expression")
				r.append("%s%s._setId('%s')" %(ident, parent, value))
			elif target.endswith(".id"):
				raise Exception("setting id of the remote object is prohibited")
			else:
				self.check_target_property(registry, target)

			if isinstance(value, component_generator):
				if target != "delegate":
					var = "%s$%s" %(escape(parent), escape(target))
					r.append("//creating component %s" %value.name)
					r.append("%svar %s = new _globals.%s(%s)" %(ident, var, registry.find_component(value.package, value.component.name), parent))
					r.append("%s%s.%s = %s" %(ident, closure, var, var))
					code = self.call_create(registry, ident_n, var, value, closure)
					r.append(code)
					r.append('%s%s.%s = %s' %(ident, parent, target, var))
				else:
					code = "%svar delegate = new _globals.%s(%s, true)\n" %(ident, registry.find_component(value.package, value.component.name), parent)
					code += "%svar __closure = { delegate: delegate }\n" %(ident)
					code += self.call_create(registry, ident_n + 1, 'delegate', value, '__closure') + '\n'
					code += self.call_setup(registry, ident_n + 1, 'delegate', value, '__closure') + '\n'
					r.append("%s%s.%s = (function() {\n%s\n%sreturn delegate\n}).bind(%s)" %(ident, parent, target, code, ident, parent))

		for name, target in self.aliases.iteritems():
			get, pname = generate_accessors(target)
			r.append("%score.addAliasProperty(%s, '%s', (function() { return %s; }).bind(%s), '%s')" \
				%(ident, parent, name, get, parent, pname))

		return "\n".join(r)

	def get_rvalue(self, parent, target):
		path = target.split(".")
		path = ["_get('%s')" %x for x in path]
		return "%s.%s" % (parent, ".".join(path))

	def get_lvalue(self, parent, target):
		path = target.split(".")
		path = ["_get('%s')" %x for x in path[:-1]] + [path[-1]]
		return "%s.%s" % (parent, ".".join(path))


	def generate_setup_code(self, registry, parent, closure, ident_n = 1):
		r = []
		ident = "\t" * ident_n

		for target, value in self.assignments.iteritems():
			if target == "id":
				continue
			t = type(value)
			#print self.name, target, value
			target_lvalue = self.get_lvalue(parent, target)
			if t is str:
				value = replace_enums(value, self, registry)
				deps = parse_deps(parent, value)
				if deps:
					var = "update$%s$%s" %(escape(parent), escape(target))
					r.append('//assigning %s to %s' %(target, value))
					r.append("%svar %s = (function() { %s = (%s); }).bind(%s)" %(ident, var, target_lvalue, value, parent))
					undep = []
					for idx, _dep in enumerate(deps):
						path, dep = _dep
						depvar = "dep$%s$%s$%d" %(escape(parent), escape(target), idx)
						r.append('%svar %s = %s' %(ident, depvar, path))
						r.append("%s%s.connectOnChanged(%s, '%s', %s)" %(ident, parent, depvar, dep, var))
						undep.append("[%s, '%s', %s]" %(depvar, dep, var))
					r.append("%s%s._removeUpdater('%s', [%s])" %(ident, parent, target, ",".join(undep)))
					r.append("%s%s();" %(ident, var))
				else:
					r.append('//assigning %s to %s' %(target, value))
					r.append("%s%s._removeUpdater('%s'); %s = (%s);" %(ident, parent, target, target_lvalue, value))

			elif t is component_generator:
				if target == "delegate":
					continue
				var = "%s$%s" %(escape(parent), escape(target))
				r.append(self.call_setup(registry, ident_n, var, value, closure))
			else:
				raise Exception("skip assignment %s = %s" %(target, value))

		if not self.prototype:
			for _name, argscode in self.methods.iteritems():
				path, name = _name
				args, code = argscode
				path = path_or_parent(path, parent)
				code = process(code, self, registry)
				r.append("%s%s.%s = (function(%s) %s ).bind(%s)" %(ident, path, name, ",".join(args), code, path))

		for _name, argscode in self.signal_handlers.iteritems():
			path, name = _name
			if not path and self.prototype and name != 'completed': #sync with condition above
				continue
			args, code = argscode
			code = process(code, self, registry)
			path = path_or_parent(path, parent)
			if name != "completed":
				r.append("%s%s.on('%s', (function(%s) %s ).bind(%s))" %(ident, path, name, ",".join(args), code, path))
			else:
				r.append("%s%s._context._onCompleted((function() %s ).bind(%s))" %(ident, path, code, path))

		for _name, code in self.changed_handlers.iteritems():
			path, name = _name
			if not path and self.prototype: #sync with condition above
				continue
			code = process(code, self, registry)
			path = path_or_parent(path, parent)
			r.append("%s%s.onChanged('%s', (function(value) %s ).bind(%s))" %(ident, path, name, code, path))

		for _name, code in self.key_handlers.iteritems():
			path, name = _name
			if not path and self.prototype: #sync with condition above
				continue
			code = process(code, self, registry)
			path = path_or_parent(path, parent)
			r.append("%s%s.onPressed('%s', (function(key, event) %s ).bind(%s))" %(ident, path, name, code, path))

		r.append(self.generate_animations(registry, parent))

		for idx, value in enumerate(self.children):
			var = '%s$child%d' %(escape(parent), idx)
			r.append(self.call_setup(registry, ident_n, var, value, closure))
			r.append("%s%s.addChild(%s)" %(ident, parent, var));

		if self.elements:
			r.append("\t%s.assign(%s)" %(parent, json.dumps(self.elements)))

		return "\n".join(r)
