#!/usr/bin/env python2.7
# -*- coding: utf-8 -*-
import os
import sys


if not hasattr(sys,"frozen"):
    sys.path.insert(1, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'lib'))

from sslyze import __version__, PROJECT_URL
from sslyze.cli.command_line_parser import CommandLineParsingError, CommandLineParser
import re
import json
import signal
from multiprocessing import freeze_support
from time import time
from xml.dom import minidom
from xml.etree.ElementTree import Element, tostring
from sslyze.plugins_process_pool import PluginsProcessPool
from sslyze.plugins_finder import PluginsFinder
from sslyze.server_connectivity import ClientAuthenticationServerConfigurationEnum
from sslyze.server_connectivity import ServerConnectivityError, ServersConnectivityTester
from sslyze.ssl_settings import TlsWrappedProtocolEnum

# Global so we can terminate processes when catching SIGINT
plugins_process_pool = None


# Todo: Move formatting stuff to another file
SCAN_FORMAT = u'Scan Results For {0}:{1} - {2}:{1}'


def _format_title(title):
    return u' {title}\n {underline}\n'.format(title=title.upper(), underline='-' * len(title))


TLS_PROTOCOL_XML_TEXT = {
    TlsWrappedProtocolEnum.PLAIN_TLS: 'plainTls',
    TlsWrappedProtocolEnum.HTTPS: 'https',
    TlsWrappedProtocolEnum.STARTTLS_SMTP: 'startTlsSmtp',
    TlsWrappedProtocolEnum.STARTTLS_XMPP: 'startTlsXmpp',
    TlsWrappedProtocolEnum.STARTTLS_XMPP_SERVER: 'startTlsXmppServer',
    TlsWrappedProtocolEnum.STARTTLS_POP3: 'startTlsPop3',
    TlsWrappedProtocolEnum.STARTTLS_IMAP: 'startTlsImap',
    TlsWrappedProtocolEnum.STARTTLS_FTP: 'startTlsFtp',
    TlsWrappedProtocolEnum.STARTTLS_LDAP: 'startTlsLdap',
    TlsWrappedProtocolEnum.STARTTLS_RDP: 'startTlsRdp',
    TlsWrappedProtocolEnum.STARTTLS_POSTGRES: 'startTlsPostGres',
}


def _format_xml_target_result(server_info, result_list):
    target_attrib = {'host': server_info.hostname,
                     'ip': server_info.ip_address,
                     'port': str(server_info.port),
                     'tlsWrappedProtocol': TLS_PROTOCOL_XML_TEXT[server_info.tls_wrapped_protocol]
                     }
    if server_info.http_tunneling_settings:
        # Add proxy settings
        target_attrib['httpsTunnelHostname'] = server_info.http_tunneling_settings.hostname
        target_attrib['httpsTunnelPort'] = str(server_info.http_tunneling_settings.port)

    target_xml = Element('target', attrib=target_attrib)
    result_list.sort(key=lambda result: result)  # Sort results

    for plugin_result in result_list:
        target_xml.append(plugin_result.as_xml())

    return target_xml


def _object_to_json_dict(plugin_object):
    """Convert an object to a dictionnary suitable for the JSON output.
    """
    final_fict = {}
    for key, value in plugin_object.__dict__.iteritems():
        if not key.startswith('_'):
            # Remove private attributes
            final_fict[key] = value
    return final_fict



def _format_json_result(server_info, result_list):
    dict_final = {'server_info': server_info.__dict__}
    dict_command_result = {}
    for plugin_result in result_list:
        dict_result = plugin_result.__dict__
        # Remove the server_info node
        dict_result.pop("server_info", None)
        # Remove the plugin_command node
        plugin_command = dict_result.pop("plugin_command", None)
        dict_command_result[plugin_command] = dict_result

    dict_final['commands_results'] = dict_command_result

    return dict_final



def _format_txt_target_result(server_info, result_list):
    target_result_str = u''

    for plugin_result in result_list:
        # Print the result of each separate command
        target_result_str += '\n'
        for line in plugin_result.as_text():
            target_result_str += line + '\n'

    scan_txt = SCAN_FORMAT.format(server_info.hostname, str(server_info.port), server_info.ip_address)
    return _format_title(scan_txt) + target_result_str + '\n\n'


def sigint_handler(signum, frame):
    print 'Scan interrupted... shutting down.'
    if plugins_process_pool:
        plugins_process_pool.emergency_shutdown()
    sys.exit()


def main():
    # For py2exe builds
    freeze_support()

    # Handle SIGINT to terminate processes
    signal.signal(signal.SIGINT, sigint_handler)

    start_time = time()
    #--PLUGINS INITIALIZATION--
    sslyze_plugins = PluginsFinder()
    available_plugins = sslyze_plugins.get_plugins()
    available_commands = sslyze_plugins.get_commands()

    # Create the command line parser and the list of available options
    sslyze_parser = CommandLineParser(available_plugins, __version__)

    online_servers_list = []
    invalid_servers_list = []

    # Parse the command line
    try:
        good_server_list, bad_server_list, args_command_list = sslyze_parser.parse_command_line()
        invalid_servers_list.extend(bad_server_list)
    except CommandLineParsingError as e:
        print e.get_error_msg()
        return

    should_print_text_results = not args_command_list.quiet and args_command_list.xml_file != '-'  \
        and args_command_list.json_file != '-'
    if should_print_text_results:
        print '\n\n\n' + _format_title('Available plugins')
        for plugin in available_plugins:
            print '  ' + plugin.__name__
        print '\n\n'


    #--PROCESSES INITIALIZATION--
    if args_command_list.https_tunnel:
        # Maximum one process to not kill the proxy
        plugins_process_pool = PluginsProcessPool(sslyze_plugins, args_command_list.nb_retries,
                                                  args_command_list.timeout, max_processes_nb=1)
    else:
        plugins_process_pool = PluginsProcessPool(sslyze_plugins, args_command_list.nb_retries,
                                                  args_command_list.timeout)

    #--TESTING SECTION--
    # Figure out which hosts are up and fill the task queue with work to do
    if should_print_text_results:
        print _format_title('Checking host(s) availability')

    connectivity_tester = ServersConnectivityTester(good_server_list)
    connectivity_tester.start_connectivity_testing(network_timeout=args_command_list.timeout)

    SERVER_OK_FORMAT = u'   {host}:{port:<25} => {ip_address} {client_auth_msg}'
    SERVER_INVALID_FORMAT = u'   {server_string:<35} => WARNING: {error_msg}; discarding corresponding tasks.'

    # Store and print servers we were able to connect to
    for server_connectivity_info in connectivity_tester.get_reachable_servers():
        online_servers_list.append(server_connectivity_info)
        if should_print_text_results:
            client_auth_msg = ''
            client_auth_requirement = server_connectivity_info.client_auth_requirement
            if client_auth_requirement == ClientAuthenticationServerConfigurationEnum.REQUIRED:
                client_auth_msg = '  WARNING: Server REQUIRED client authentication, specific plugins will fail.'
            elif client_auth_requirement == ClientAuthenticationServerConfigurationEnum.OPTIONAL:
                client_auth_msg = '  WARNING: Server requested optional client authentication'

            print SERVER_OK_FORMAT.format(host=server_connectivity_info.hostname, port=server_connectivity_info.port,
                                          ip_address=server_connectivity_info.ip_address,
                                          client_auth_msg=client_auth_msg)

        # Send tasks to worker processes
        for plugin_command in available_commands:
            if getattr(args_command_list, plugin_command):
                # Get this plugin's options if there's any
                plugin_options_dict = {}
                for option in available_commands[plugin_command].get_interface().get_options():
                    # Was this option set ?
                    if getattr(args_command_list,option.dest):
                        plugin_options_dict[option.dest] = getattr(args_command_list, option.dest)

                plugins_process_pool.queue_plugin_task(server_connectivity_info, plugin_command, plugin_options_dict)


    for tentative_server_info, exception in connectivity_tester.get_invalid_servers():
        invalid_servers_list.append((tentative_server_info.server_string, exception))


    # Print servers we were NOT able to connect to
    if should_print_text_results:
        for server_string, exception in invalid_servers_list:
            if isinstance(exception, ServerConnectivityError):
                print SERVER_INVALID_FORMAT.format(server_string=server_string, error_msg=exception.error_msg)
            else:
                # Unexpected bug in SSLyze
                raise exception
        print '\n\n'

    # Keep track of how many tasks have to be performed for each target
    task_num = 0
    for command in available_commands:
        if getattr(args_command_list, command):
            task_num += 1


    # --REPORTING SECTION--
    # XML output
    xml_output_list = []

    # Each host has a list of results
    result_dict = {}
    # We cannot use the server_info object directly as its address will change due to multiprocessing
    RESULT_KEY_FORMAT = u'{hostname}:{ip_address}:{port}'.format
    for server_info in online_servers_list:
        result_dict[RESULT_KEY_FORMAT(hostname=server_info.hostname, ip_address=server_info.ip_address,
                                      port=server_info.port)] = []

    # Process the results as they come
    for plugin_result in plugins_process_pool.get_results():
        server_info = plugin_result.server_info
        result_dict[RESULT_KEY_FORMAT(hostname=server_info.hostname, ip_address=server_info.ip_address,
                                      port=server_info.port)].append(plugin_result)

        result_list = result_dict[RESULT_KEY_FORMAT(hostname=server_info.hostname, ip_address=server_info.ip_address,
                                                    port=server_info.port)]

        if len(result_list) == task_num:
            # Done with this server; print the results and update the xml doc
            if args_command_list.xml_file:
                xml_output_list.append(_format_xml_target_result(server_info, result_list))

            if should_print_text_results:
                print _format_txt_target_result(server_info, result_list)


    # --TERMINATE--
    exec_time = time()-start_time

    # Output JSON to a file if needed
    if args_command_list.json_file:
        json_output = {'total_scan_time': str(exec_time),
                       'network_timeout': str(args_command_list.timeout),
                       'network_max_retries': str(args_command_list.nb_retries),
                       'invalid_targets': [],
                       'accepted_targets': []}

        # Add the list of invalid targets
        for server_string, exception in invalid_servers_list:
            if isinstance(exception, ServerConnectivityError):
                json_output['invalid_targets'].append({server_string: exception.error_msg})
            else:
                # Unexpected bug in SSLyze
                raise exception

        # Add the output of the plugins for each server
        for host_str, plugin_result_list in result_dict.iteritems():
            server_info = plugin_result_list[0].server_info
            json_output['accepted_targets'].append(_format_json_result(server_info, plugin_result_list))

        final_json_output = json.dumps(json_output, default=_object_to_json_dict, sort_keys=True, indent=4)
        if args_command_list.json_file == '-':
            # Print XML output to the console if needed
            print final_json_output
        else:
            # Otherwise save the XML output to the console
            with open(args_command_list.json_file, 'w') as json_file:
                json_file.write(final_json_output)


    # Output XML doc to a file if needed
    if args_command_list.xml_file:
        result_xml_attr = {'totalScanTime': str(exec_time),
                           'networkTimeout': str(args_command_list.timeout),
                           'networkMaxRetries': str(args_command_list.nb_retries)}
        result_xml = Element('results', attrib = result_xml_attr)

        # Sort results in alphabetical order to make the XML files (somewhat) diff-able
        xml_output_list.sort(key=lambda xml_elem: xml_elem.attrib['host'])
        for xml_element in xml_output_list:
            result_xml.append(xml_element)

        xml_final_doc = Element('document', title="SSLyze Scan Results", SSLyzeVersion=__version__,
                                SSLyzeWeb=PROJECT_URL)

        # Add the list of invalid targets
        invalid_targets_xml = Element('invalidTargets')
        for server_string, exception in invalid_servers_list:
            if isinstance(exception, ServerConnectivityError):
                error_xml = Element('invalidTarget', error=exception.error_msg)
                error_xml.text = server_string
                invalid_targets_xml.append(error_xml)
            else:
                # Unexpected bug in SSLyze
                raise exception
        xml_final_doc.append(invalid_targets_xml)

        # Add the output of the plugins
        xml_final_doc.append(result_xml)

        # Remove characters that are illegal for XML
        # https://lsimons.wordpress.com/2011/03/17/stripping-illegal-characters-out-of-xml-in-python/
        xml_final_string = tostring(xml_final_doc, encoding='UTF-8')
        illegal_xml_chars_RE = re.compile(u'[\x00-\x08\x0b\x0c\x0e-\x1F\uD800-\uDFFF\uFFFE\uFFFF]')
        xml_sanitized_final_string = illegal_xml_chars_RE.sub('', xml_final_string)

        # Hack: Prettify the XML file so it's (somewhat) diff-able
        xml_final_pretty = minidom.parseString(xml_sanitized_final_string).toprettyxml(indent="  ", encoding="utf-8" )

        if args_command_list.xml_file == '-':
            # Print XML output to the console if needed
            print xml_final_pretty
        else:
            # Otherwise save the XML output to the console
            with open(args_command_list.xml_file, 'w') as xml_file:
                xml_file.write(xml_final_pretty)


    if should_print_text_results:
        print _format_title('Scan Completed in {0:.2f} s'.format(exec_time))


if __name__ == "__main__":
    main()
