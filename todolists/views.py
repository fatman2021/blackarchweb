import json

from django import forms
from django.http import HttpResponse
from django.core.mail import send_mail
from django.shortcuts import get_list_or_404, get_object_or_404, redirect, render
from django.db import transaction
from django.views.decorators.cache import never_cache
from django.views.generic import DeleteView
from django.template import Context, loader

from main.models import Package, Repo
from packages.utils import attach_maintainers
from .models import Todolist, TodolistPackage
from .utils import get_annotated_todolists


class TodoListForm(forms.ModelForm):
    raw = forms.CharField(label='Packages', required=False,
            help_text='(one per line)',
            widget=forms.Textarea(attrs={'rows': '20', 'cols': '60'}))

    def packages(self):
        package_names = {s.strip() for s in
                self.cleaned_data['raw'].split("\n")}
        return Package.objects.normal().filter(pkgname__in=package_names,
                repo__testing=False, repo__staging=False).order_by('arch')

    class Meta:
        model = Todolist
        fields = ('name', 'description', 'raw')


@never_cache
def flag(request, slug, pkg_id):
    todolist = get_object_or_404(Todolist, slug=slug)
    tlpkg = get_object_or_404(TodolistPackage, id=pkg_id)
    # TODO: none of this; require absolute value on submit
    if tlpkg.status == TodolistPackage.INCOMPLETE:
        tlpkg.status = TodolistPackage.COMPLETE
    else:
        tlpkg.status = TodolistPackage.INCOMPLETE
    tlpkg.save()
    if request.is_ajax():
        data = {
            'status': tlpkg.get_status_display(),
            'css_class': tlpkg.status_css_class(),
        }
        return HttpResponse(json.dumps(data), mimetype='application/json')
    return redirect(todolist)


def view_redirect(request, old_id):
    todolist = get_object_or_404(Todolist, old_id=old_id)
    return redirect(todolist, permanent=True)


def view(request, slug):
    todolist = get_object_or_404(Todolist, slug=slug)
    svn_roots = Repo.objects.values_list(
            'svn_root', flat=True).order_by().distinct()
    # we don't hold onto the result, but the objects are the same here,
    # so accessing maintainers in the template is now cheap
    attach_maintainers(todolist.packages())
    arches = {tp.arch for tp in todolist.packages()}
    repos = {tp.repo for tp in todolist.packages()}
    return render(request, 'todolists/view.html', {
        'list': todolist,
        'svn_roots': svn_roots,
        'arches': sorted(arches),
        'repos': sorted(repos),
    })


# really no need for login_required on this one...
def list_pkgbases(request, slug, svn_root):
    '''Used to make bulk moves of packages a lot easier.'''
    todolist = get_object_or_404(Todolist, slug=slug)
    repos = get_list_or_404(Repo, svn_root=svn_root)
    pkgbases = TodolistPackage.objects.values_list('pkgbase', flat=True).filter(
            todolist=todolist, repo__in=repos).distinct().order_by('pkgbase')
    return HttpResponse('\n'.join(pkgbases),
        mimetype='text/plain')


def todolist_list(request):
    lists = get_annotated_todolists()
    return render(request, 'todolists/list.html', {'lists': lists})


@never_cache
def add(request):
    if request.POST:
        form = TodoListForm(request.POST)
        if form.is_valid():
            new_packages = create_todolist_packages(form, creator=request.user)
            send_todolist_emails(form.instance, new_packages)
            return redirect(form.instance)
    else:
        form = TodoListForm()

    page_dict = {
            'title': 'Add Todo List',
            'description': '',
            'form': form,
            'submit_text': 'Create List'
    }
    return render(request, 'general_form.html', page_dict)


# TODO: this calls for transaction management and async emailing
@never_cache
def edit(request, slug):
    todo_list = get_object_or_404(Todolist, slug=slug)
    if request.POST:
        form = TodoListForm(request.POST, instance=todo_list)
        if form.is_valid():
            new_packages = create_todolist_packages(form)
            send_todolist_emails(todo_list, new_packages)
            return redirect(todo_list)
    else:
        form = TodoListForm(instance=todo_list,
                initial={ 'packages': todo_list.raw })

    page_dict = {
            'title': 'Edit Todo List: %s' % todo_list.name,
            'description': '',
            'form': form,
            'submit_text': 'Save List'
    }
    return render(request, 'general_form.html', page_dict)


class DeleteTodolist(DeleteView):
    model = Todolist
    # model in main == assumes name 'main/todolist_confirm_delete.html'
    template_name = 'todolists/todolist_confirm_delete.html'
    success_url = '/todo/'


@transaction.commit_on_success
def create_todolist_packages(form, creator=None):
    packages = form.packages()
    if creator:
        # todo list is new
        todolist = form.save(commit=False)
        todolist.creator = creator
        todolist.save()

        old_packages = []
    else:
        # todo list already existed
        form.save()
        todolist = form.instance

        # first delete any packages not in the new list
        for todo_pkg in todolist.packages():
            if todo_pkg.pkg not in packages:
                todo_pkg.delete()

        # save the old package list so we know what to add
        old_packages = [todo_pkg.pkg for todo_pkg in todolist.packages()]

    todo_pkgs = []
    for package in packages:
        if package not in old_packages:
            todo_pkg = TodolistPackage.objects.create(todolist=todolist,
                    pkg=package, pkgname=package.pkgname,
                    pkgbase=package.pkgbase,
                    arch=package.arch, repo=package.repo)
            todo_pkgs.append(todo_pkg)

    return todo_pkgs


def send_todolist_emails(todo_list, new_packages):
    '''Sends emails to package maintainers notifying them that packages have
    been added to a todo list.'''
    # start by flipping the incoming list on its head: we want a list of
    # involved maintainers and the packages they need to be notified about.
    orphan_packages = []
    maint_packages = {}
    for todo_package in new_packages:
        maints = todo_package.pkg.maintainers.values_list('email', flat=True)
        if not maints:
            orphan_packages.append(todo_package)
        else:
            for maint in maints:
                maint_packages.setdefault(maint, []).append(todo_package)

    for maint, packages in maint_packages.iteritems():
        ctx = Context({
            'todo_packages': sorted(packages),
            'todolist': todo_list,
        })
        template = loader.get_template('todolists/email_notification.txt')
        send_mail('Packages added to todo list \'%s\'' % todo_list.name,
                template.render(ctx),
                'Arch Website Notification <nobody@archlinux.org>',
                [maint],
                fail_silently=True)


def public_list(request):
    todo_lists = Todolist.objects.incomplete()
    # total hackjob, but it makes this a lot less query-intensive.
    all_pkgs = [tp for tl in todo_lists for tp in tl.packages()]
    attach_maintainers(all_pkgs)
    return render(request, "todolists/public_list.html",
            {"todo_lists": todo_lists})

# vim: set ts=4 sw=4 et:
